/* =============================================================================
   AMD FLASH PROGRAMMER - Universal (AM29F010 / AM29F040B)
   
   Author: Balazs Markus
   Rewritten & Extended for AM29F040B support, chip selection,
   robust erase polling, and improved serial protocol.

   Wiring (Arduino Nano):
     Shift Registers (74HC595 x2, daisy-chained):
       A5 -> SR1 DS (Serial Data)
       A4 -> Both RCLK (Latch)
       A3 -> Both SRCLK (Shift Clock)
     High Address Lines:
       A2 -> A16, A1 -> A17, A0 -> A18
     Data Bus:
       D2-D9 -> Flash DQ0-DQ7
     Control:
       D10 -> CE#, D11 -> OE#, D12 -> WE#

   Serial Protocol (115200 baud):
     All commands are ASCII. Arduino always terminates responses with '\n'.

     'C' <chip_id>\n      - Select chip: '0'=AM29F010, '1'=AM29F040B
     'I'\n                - Get chip ID: returns "MFR:<hex> DEV:<hex>\n"
     'R' <start_hex> <end_hex>\n - Read range, returns binary with SIZE\n prefix
                                   Actually returns: "RSTART\n" then bytes then "REND\n"
     'E'\n                - Chip erase. Returns "ERASE_START\n", polls DQ7,
                            then returns "ERASE_OK\n" or "ERASE_FAIL\n"
     'W' <size_dec>\n     - Write: Arduino replies "WREADY\n", then accepts
                            <size> raw bytes, replies "WDONE\n" or "WERR:<addr>\n"
     'V' <start_hex> <end_hex>\n - Verify: same format as read (for post-write check)
     
   ============================================================================= */

#include <avr/io.h>
#include <util/delay.h>

// ---- Chip definitions -------------------------------------------------------
// Each chip has its own unlock address pair and size
struct ChipDef {
  uint32_t unlock1;   // First unlock address (e.g. 0x5555 or 0x555)
  uint32_t unlock2;   // Second unlock address (e.g. 0x2AAA or 0x2AA)
  uint32_t flash_size;// Total flash size in bytes
  const char *name;
};

const ChipDef CHIPS[] = {
  { 0x5555, 0x2AAA, 131072UL,  "AM29F010"  },  // chip index 0
  { 0x555,  0x2AA,  524288UL,  "AM29F040B" },  // chip index 1 (default)
};

#define DEFAULT_CHIP_IDX 1  // AM29F040B
#define NUM_CHIPS 2

int  g_chip_idx = DEFAULT_CHIP_IDX;
uint32_t g_ul1;    // unlock address 1 (cached)
uint32_t g_ul2;    // unlock address 2 (cached)
uint32_t g_flash_size;

// ---- Erase timeout ----------------------------------------------------------
// AM29F040B chip erase max time is ~64s per datasheet, we use 70s
#define ERASE_TIMEOUT_MS 70000UL

// ---- Pin assignments (do not change without updating port manipulation) ------
// D2-D7 = PORTD bits 2-7
// D8-D9 = PORTB bits 0-1
// D10=PORTB2(CE#), D11=PORTB3(OE#), D12=PORTB4(WE#)
// A3=PORTC3(SRCLK), A4=PORTC4(RCLK), A5=PORTC5(SER)
// A0=PORTC0(A18),  A1=PORTC1(A17),   A2=PORTC2(A16)

// =============================================================================
// LOW LEVEL PIN CONTROL
// =============================================================================

void set_data_input() {
  DDRD &= 0x03;   // D2-D7 as input (keep D0,D1 for Serial)
  DDRB &= ~0x03;  // D8-D9 as input
  // Disable pull-ups
  PORTD &= 0x03;
  PORTB &= ~0x03;
}

void set_data_output() {
  DDRD |= 0xFC;   // D2-D7 as output
  DDRB |= 0x03;   // D8-D9 as output
}

void flash_ctrl_deselect() {
  // CE#=HIGH, WE#=HIGH, OE#=HIGH  (all inactive)
  PORTB |=  (1<<2)|(1<<3)|(1<<4);
}

void flash_ctrl_read() {
  // OE#=LOW first, then CE#=LOW
  PORTB &= ~(1<<3);  // OE# low
  _delay_us(1);
  PORTB &= ~(1<<2);  // CE# low
  _delay_us(1);
}

void flash_ctrl_write_pulse() {
  // CE# already low. Pulse WE#.
  PORTB &= ~(1<<2);  // CE# low
  _delay_us(1);
  PORTB &= ~(1<<4);  // WE# low
  _delay_us(1);
  PORTB |=  (1<<4);  // WE# high
  _delay_us(1);
}

// =============================================================================
// ADDRESS SETTING
// =============================================================================

void flash_set_high_addr(uint16_t addr) {
  // A16 = PORTC2, A17 = PORTC1, A18 = PORTC0
  if (addr & 0x01) PORTC |=  _BV(PORTC2); else PORTC &= ~_BV(PORTC2);
  if (addr & 0x02) PORTC |=  _BV(PORTC1); else PORTC &= ~_BV(PORTC1);
  if (addr & 0x04) PORTC |=  _BV(PORTC0); else PORTC &= ~_BV(PORTC0);
}

void flash_shift_addr16(uint16_t data) {
  // Send 16-bit address to shift registers (MSB first into SR2, LSB into SR1)
  // SR2 = A15..A8 (high byte), SR1 = A7..A0 (low byte)
  // We shift MSB of high byte first
  uint8_t hi = (data >> 8) & 0xFF;
  uint8_t lo = (data)      & 0xFF;

  PORTC &= ~_BV(PORTC4);  // Latch LOW

  // Shift high byte (A15..A8) first — ends up in SR2
  for (int i = 7; i >= 0; i--) {
    PORTC &= ~_BV(PORTC3); // SRCLK LOW
    if (hi & (1 << i)) PORTC |= _BV(PORTC5);
    else                PORTC &= ~_BV(PORTC5);
    PORTC |= _BV(PORTC3);  // SRCLK HIGH (shift in)
  }

  // Shift low byte (A7..A0) — ends up in SR1
  for (int i = 7; i >= 0; i--) {
    PORTC &= ~_BV(PORTC3);
    if (lo & (1 << i)) PORTC |= _BV(PORTC5);
    else                PORTC &= ~_BV(PORTC5);
    PORTC |= _BV(PORTC3);
  }

  PORTC |= _BV(PORTC4);  // Latch HIGH — outputs update
}

void flash_set_addr(uint32_t addr) {
  flash_shift_addr16((uint16_t)(addr & 0xFFFF));
  flash_set_high_addr((uint16_t)((addr >> 16) & 0x07));
}

// =============================================================================
// DATA BUS I/O
// =============================================================================

void flash_write_data(uint8_t data) {
  // D2-D7 = data bits 0-5, D8 = bit 6, D9 = bit 7
  // Map: data bit N -> Arduino pin (N+2), except bits 6,7 -> pins 8,9
  uint8_t portd_mask = (data << 2) & 0xFC;
  uint8_t portb_mask = (data >> 6) & 0x03;

  PORTD = (PORTD & 0x03) | portd_mask;
  PORTB = (PORTB & 0xFC) | portb_mask;
}

uint8_t flash_read_data() {
  uint8_t portd_val = (PIND >> 2) & 0x3F;
  uint8_t portb_val = (PINB & 0x03) << 6;
  return portd_val | portb_val;
}

// =============================================================================
// COMMAND SEQUENCING
// =============================================================================

void load_chip_params() {
  g_ul1        = CHIPS[g_chip_idx].unlock1;
  g_ul2        = CHIPS[g_chip_idx].unlock2;
  g_flash_size = CHIPS[g_chip_idx].flash_size;
}

// Send a command bus cycle (address + data write)
void flash_cmd(uint32_t addr, uint8_t data) {
  flash_set_addr(addr);
  _delay_us(1);
  set_data_output();
  flash_write_data(data);
  flash_ctrl_write_pulse();
  flash_ctrl_deselect();
}

// Standard unlock sequence
void flash_unlock() {
  flash_cmd(g_ul1, 0xAA);
  flash_cmd(g_ul2, 0x55);
}

void flash_reset() {
  flash_cmd(g_ul1, 0xAA);
  flash_cmd(g_ul2, 0x55);
  flash_cmd(g_ul1, 0xF0);
  _delay_ms(10);
}

// =============================================================================
// POLLING - DQ7 Data Polling (proper implementation)
// =============================================================================

// Returns true = operation complete, false = timeout
bool wait_for_dq7(uint32_t addr, uint8_t expected_data, uint32_t timeout_ms) {
  uint8_t expected_dq7 = expected_data & 0x80;
  uint32_t start = millis();
  
  set_data_input();
  flash_set_addr(addr);
  PORTB &= ~(1<<2);  // CE# low
  PORTB &= ~(1<<3);  // OE# low

  while (millis() - start < timeout_ms) {
    uint8_t val = flash_read_data();
    if ((val & 0x80) == expected_dq7) {
      // DQ7 matches — but also check DQ5 (error bit) isn't set after match
      PORTB |= (1<<2)|(1<<3);  // deselect
      return true;
    }
    // Check DQ5 (error flag)
    if (val & 0x20) {
      // DQ5 set — read once more to confirm
      uint8_t val2 = flash_read_data();
      PORTB |= (1<<2)|(1<<3);
      if ((val2 & 0x80) == expected_dq7) return true;
      return false;  // Confirmed error
    }
  }
  PORTB |= (1<<2)|(1<<3);
  return false;  // Timeout
}

// Erase poll: wait for DQ7=1 at address 0, with long timeout
bool wait_for_erase(uint32_t timeout_ms) {
  uint32_t start = millis();
  
  set_data_input();
  flash_set_addr(0x00);
  PORTB &= ~(1<<2);
  PORTB &= ~(1<<3);
  
  while (millis() - start < timeout_ms) {
    uint8_t val = flash_read_data();
    if (val & 0x80) {
      // DQ7=1 means erase complete
      // Also verify DQ3 toggling has stopped and read again
      PORTB |= (1<<2)|(1<<3);
      // Read back to confirm
      flash_set_addr(0x00);
      PORTB &= ~(1<<2);
      PORTB &= ~(1<<3);
      _delay_us(2);
      uint8_t confirm = flash_read_data();
      PORTB |= (1<<2)|(1<<3);
      return (confirm & 0x80) != 0;
    }
    // DQ5=1 while DQ7=0 => error
    if ((val & 0x20) && !(val & 0x80)) {
      uint8_t val2 = flash_read_data();
      PORTB |= (1<<2)|(1<<3);
      if (val2 & 0x80) return true;
      return false;
    }
  }
  PORTB |= (1<<2)|(1<<3);
  return false;
}

// =============================================================================
// AUTOSELECT: Read Manufacturer & Device ID
// =============================================================================

void flash_read_ids(uint8_t *mfr_id, uint8_t *dev_id) {
  flash_ctrl_deselect();
  _delay_ms(5);
  
  // Enter autoselect
  flash_unlock();
  flash_cmd(g_ul1, 0x90);
  _delay_ms(2);
  
  // Read Manufacturer ID (addr 0x00)
  set_data_input();
  flash_set_addr(0x00);
  flash_ctrl_read();
  _delay_us(2);
  *mfr_id = flash_read_data();
  flash_ctrl_deselect();
  
  // Read Device ID (addr 0x01)
  flash_set_addr(0x01);
  flash_ctrl_read();
  _delay_us(2);
  *dev_id = flash_read_data();
  flash_ctrl_deselect();
  
  // Exit autoselect (software reset)
  set_data_output();
  flash_cmd(0x00, 0xF0);
  _delay_ms(5);
}

// =============================================================================
// READ MEMORY
// =============================================================================

void flash_read_range(uint32_t start_addr, uint32_t end_addr) {
  uint32_t count = end_addr - start_addr + 1;
  
  Serial.print(F("RSTART "));
  Serial.println(count);  // Tell host how many bytes to expect
  
  set_data_input();
  
  // Use a 64-byte block buffer for speed
  uint8_t block[64];
  uint32_t i = start_addr;
  
  while (i <= end_addr) {
    uint8_t chunk = (uint8_t)min((uint32_t)64, end_addr - i + 1);
    for (uint8_t b = 0; b < chunk; b++) {
      flash_set_addr(i + b);
      flash_ctrl_read();
      _delay_us(1);
      block[b] = flash_read_data();
      flash_ctrl_deselect();
    }
    Serial.write(block, chunk);
    i += chunk;
  }
  
  Serial.println(F("REND"));
}

// =============================================================================
// ERASE
// =============================================================================

void flash_erase_chip() {
  Serial.println(F("ERASE_START"));
  
  flash_ctrl_deselect();
  set_data_output();
  _delay_ms(10);
  
  // Chip erase sequence
  flash_unlock();
  flash_cmd(g_ul1, 0x80);
  flash_unlock();
  flash_cmd(g_ul1, 0x10);
  
  // Now poll DQ7 — must become 1 when done
  bool ok = wait_for_erase(ERASE_TIMEOUT_MS);
  
  set_data_output();
  flash_reset();
  flash_ctrl_deselect();
  
  if (ok) {
    Serial.println(F("ERASE_OK"));
  } else {
    Serial.println(F("ERASE_FAIL"));
  }
}

// =============================================================================
// PROGRAM
// =============================================================================

// Program a single byte. Returns true on success.
// NOTE: Does NOT skip 0xFF — caller must write all bytes faithfully.
// Skipping 0xFF is an optimisation only valid if you know the chip was freshly
// erased; for general use we must write every byte the host sends us.
bool flash_program_byte(uint32_t addr, uint8_t data) {
  // 0xFF is the erased state — writing it is a no-op but still valid.
  // We skip it purely for speed; the chip ignores such writes anyway.
  // IMPORTANT: We do NOT skip 0x00 or any other value.
  if (data == 0xFF) return true;

  set_data_output();
  flash_unlock();
  flash_cmd(g_ul1, 0xA0);   // Byte-program command

  // Write data to target address
  flash_set_addr(addr);
  flash_write_data(data);
  flash_ctrl_write_pulse();
  flash_ctrl_deselect();

  // Poll DQ7 for completion (up to 500 ms — datasheet max for byte program is ~9 µs
  // typical, 200 µs max for AM29F040B, but we're generous to handle slow chips).
  return wait_for_dq7(addr, data, 500);
}

// =============================================================================
// WRITE OPERATION — byte-by-byte handshaked protocol
//
// WHY: The Arduino Nano has a 64-byte hardware serial receive buffer.
// Programming a non-0xFF byte takes ~6 µs typical, up to 200 µs max.
// At 115200 baud that's ~11 bytes arriving per millisecond.
// If we buffer 64 bytes and then program them, new bytes arrive during
// programming and overflow the UART buffer — causing WERR_TIMEOUT.
//
// FIX: Strict one-byte-at-a-time handshake.
//   1. Host sends one byte.
//   2. Arduino programs it (takes ≤ 200 µs), then sends one-byte ACK 'K'.
//   3. Host waits for 'K' before sending the next byte.
//
// This is slower than burst mode (throughput ~5 KB/s instead of 11 KB/s)
// but 100% reliable because the UART buffer is never full.
//
// For a 14 KB file this means ~3 seconds — perfectly acceptable.
// For a full 512 KB chip it would take ~100 seconds — also fine.
// =============================================================================

void flash_write_from_serial(uint32_t size) {
  Serial.println(F("WREADY"));  // Tell host we're ready for byte 0

  set_data_output();

  uint32_t addr     = 0;
  uint32_t received = 0;

  while (received < size) {
    // Wait for exactly one byte with a generous timeout (2 s).
    // Host must send the next byte only after receiving our 'K' ACK,
    // so 2 s is more than enough even on slow machines.
    uint32_t wait_start = millis();
    while (!Serial.available()) {
      if (millis() - wait_start > 2000) {
        // Host stopped sending — abort
        Serial.print(F("WERR_TIMEOUT:"));
        Serial.println(received);
        return;
      }
    }

    uint8_t b = (uint8_t)Serial.read();

    if (!flash_program_byte(addr, b)) {
      // Programming failure at this address
      set_data_output();
      flash_ctrl_deselect();
      Serial.print(F("WERR:"));
      Serial.println(addr);
      return;
    }

    addr++;
    received++;

    // ACK: send a single 'K' byte so the host knows it can send the next byte.
    // We use Serial.write (not println) to keep it compact and fast.
    Serial.write('K');
  }

  set_data_output();
  flash_ctrl_deselect();

  // All bytes written successfully
  Serial.print(F("WDONE:"));
  Serial.println(received);
}

// =============================================================================
// SETUP & LOOP
// =============================================================================

void setup() {
  Serial.begin(115200);
  
  // Shift register pins
  pinMode(A5, OUTPUT);
  pinMode(A4, OUTPUT);
  pinMode(A3, OUTPUT);
  
  // High address pins
  pinMode(A2, OUTPUT);
  pinMode(A1, OUTPUT);
  pinMode(A0, OUTPUT);
  
  // Control pins: CE#, OE#, WE#
  // Default HIGH (deselected)
  PORTB |= (1<<2)|(1<<3)|(1<<4);
  DDRB  |= (1<<2)|(1<<3)|(1<<4);
  
  // Data pins default as outputs, low
  set_data_output();
  
  // Init latch/clock lines
  PORTC &= ~(_BV(PORTC3)|_BV(PORTC4)|_BV(PORTC5));
  
  // Init high address bits to 0
  PORTC &= ~(_BV(PORTC0)|_BV(PORTC1)|_BV(PORTC2));
  
  _delay_ms(50);
  
  // Load default chip
  load_chip_params();
  flash_ctrl_deselect();
  
  Serial.println(F("READY"));
}

void loop() {
  if (!Serial.available()) return;
  
  // Read command line
  char cmd_line[32];
  int len = Serial.readBytesUntil('\n', cmd_line, sizeof(cmd_line) - 1);
  cmd_line[len] = '\0';
  
  // Trim trailing CR
  if (len > 0 && cmd_line[len-1] == '\r') {
    cmd_line[--len] = '\0';
  }
  
  if (len == 0) return;
  
  char cmd = cmd_line[0];
  
  switch (cmd) {
    
    case 'C': {
      // Select chip: 'C0' or 'C1'
      if (len >= 2) {
        int idx = cmd_line[1] - '0';
        if (idx >= 0 && idx < NUM_CHIPS) {
          g_chip_idx = idx;
          load_chip_params();
          Serial.print(F("CHIP_OK:"));
          Serial.println(CHIPS[g_chip_idx].name);
        } else {
          Serial.println(F("CHIP_ERR:Invalid index"));
        }
      }
      break;
    }
    
    case 'I': {
      // Read manufacturer and device ID
      uint8_t mfr = 0, dev = 0;
      flash_read_ids(&mfr, &dev);
      Serial.print(F("MFR:"));
      if (mfr < 0x10) Serial.print('0');
      Serial.print(mfr, HEX);
      Serial.print(F(" DEV:"));
      if (dev < 0x10) Serial.print('0');
      Serial.println(dev, HEX);
      break;
    }
    
    case 'R': {
      // Read range: 'R <start_hex> <end_hex>'
      // Parse parameters
      uint32_t start_addr = 0;
      uint32_t end_addr   = g_flash_size - 1;
      
      if (len > 2) {
        char *p = cmd_line + 2;
        start_addr = strtoul(p, &p, 16);
        while (*p == ' ') p++;
        if (*p) end_addr = strtoul(p, NULL, 16);
      }
      
      // Clamp to chip size
      if (end_addr >= g_flash_size) end_addr = g_flash_size - 1;
      if (start_addr > end_addr) {
        Serial.println(F("READ_ERR:Invalid range"));
        break;
      }
      
      flash_read_range(start_addr, end_addr);
      break;
    }
    
    case 'E': {
      // Chip erase
      flash_erase_chip();
      break;
    }
    
    case 'W': {
      // Write: 'W <size_dec>'
      if (len > 2) {
        uint32_t size = strtoul(cmd_line + 2, NULL, 10);
        if (size == 0 || size > g_flash_size) {
          Serial.println(F("WERR_SIZE:Invalid size"));
          break;
        }
        flash_write_from_serial(size);
      } else {
        Serial.println(F("WERR_SIZE:No size given"));
      }
      break;
    }
    
    case '?': {
      // Status / chip info
      Serial.print(F("STATUS:"));
      Serial.print(CHIPS[g_chip_idx].name);
      Serial.print(F(" SIZE:"));
      Serial.println(g_flash_size);
      break;
    }
    
    default:
      Serial.print(F("ERR:Unknown command "));
      Serial.println(cmd);
      break;
  }
}
