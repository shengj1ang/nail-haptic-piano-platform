// Brute-force SPI pin permutation test for LIS3DH WHO_AM_I
// Test pins: 33, 34, 35, 36
// Expected WHO_AM_I for LIS3DH = 0x33

const int testPins[4] = {33, 34, 35, 36};

// Small delay for software SPI timing
inline void spiDelayShort() {
  delayMicroseconds(5);
}

// SPI MODE 3 bit-bang transfer
uint8_t spiTransferByte(int pinSCK, int pinMOSI, int pinMISO, uint8_t data) {
  uint8_t rx = 0;

  for (int i = 7; i >= 0; --i) {
    // CPOL=1 idle high
    digitalWrite(pinSCK, HIGH);
    spiDelayShort();

    // Set MOSI before falling edge
    digitalWrite(pinMOSI, (data >> i) & 0x01);
    spiDelayShort();

    // Falling edge
    digitalWrite(pinSCK, LOW);
    spiDelayShort();

    // Sample MISO
    rx <<= 1;
    if (digitalRead(pinMISO)) {
      rx |= 1;
    }

    spiDelayShort();
  }

  // Return clock to idle high
  digitalWrite(pinSCK, HIGH);
  spiDelayShort();

  return rx;
}

uint8_t readRegister(int pinCS, int pinSCK, int pinMOSI, int pinMISO, uint8_t reg) {
  digitalWrite(pinCS, LOW);
  spiDelayShort();

  spiTransferByte(pinSCK, pinMOSI, pinMISO, 0x80 | reg); // read single register
  uint8_t val = spiTransferByte(pinSCK, pinMOSI, pinMISO, 0x00);

  digitalWrite(pinCS, HIGH);
  spiDelayShort();

  return val;
}

void configurePins(int pinCS, int pinSCK, int pinMOSI, int pinMISO) {
  // First make all test pins inputs to avoid contention
  for (int i = 0; i < 4; i++) {
    pinMode(testPins[i], INPUT_PULLUP);
  }

  pinMode(pinCS, OUTPUT);
  pinMode(pinSCK, OUTPUT);
  pinMode(pinMOSI, OUTPUT);
  pinMode(pinMISO, INPUT_PULLUP);

  digitalWrite(pinCS, HIGH);
  digitalWrite(pinSCK, HIGH);  // MODE3 idle high
  digitalWrite(pinMOSI, LOW);
}

void printOneResult(int pinCS, int pinSCK, int pinMOSI, int pinMISO, uint8_t whoami) {
  Serial.print("CS=");
  Serial.print(pinCS);
  Serial.print(" SCK=");
  Serial.print(pinSCK);
  Serial.print(" MOSI=");
  Serial.print(pinMOSI);
  Serial.print(" MISO=");
  Serial.print(pinMISO);
  Serial.print(" -> WHO_AM_I=0x");
  if (whoami < 16) Serial.print("0");
  Serial.println(whoami, HEX);
}

void setup() {
  Serial.begin(115200);
  unsigned long start = millis();
  while (!Serial && millis() - start < 5000) {}

  Serial.println();
  Serial.println("=== LIS3DH brute-force SPI pin search ===");
  Serial.println("Testing all permutations of pins 33,34,35,36");
  Serial.println("Expected LIS3DH WHO_AM_I = 0x33");
  Serial.println();

  int hitCount = 0;

  // Enumerate all permutations:
  // a=CS, b=SCK, c=MOSI, d=MISO
  for (int a = 0; a < 4; a++) {
    for (int b = 0; b < 4; b++) {
      if (b == a) continue;
      for (int c = 0; c < 4; c++) {
        if (c == a || c == b) continue;
        for (int d = 0; d < 4; d++) {
          if (d == a || d == b || d == c) continue;

          int pinCS   = testPins[a];
          int pinSCK  = testPins[b];
          int pinMOSI = testPins[c];
          int pinMISO = testPins[d];

          configurePins(pinCS, pinSCK, pinMOSI, pinMISO);
          delay(20);

          // Read a few times to reduce false positives
          uint8_t v1 = readRegister(pinCS, pinSCK, pinMOSI, pinMISO, 0x0F);
          delay(2);
          uint8_t v2 = readRegister(pinCS, pinSCK, pinMOSI, pinMISO, 0x0F);
          delay(2);
          uint8_t v3 = readRegister(pinCS, pinSCK, pinMOSI, pinMISO, 0x0F);

          // Print all results
          Serial.print("CS=");
          Serial.print(pinCS);
          Serial.print(" SCK=");
          Serial.print(pinSCK);
          Serial.print(" MOSI=");
          Serial.print(pinMOSI);
          Serial.print(" MISO=");
          Serial.print(pinMISO);
          Serial.print(" -> [0x");
          if (v1 < 16) Serial.print("0");
          Serial.print(v1, HEX);
          Serial.print(", 0x");
          if (v2 < 16) Serial.print("0");
          Serial.print(v2, HEX);
          Serial.print(", 0x");
          if (v3 < 16) Serial.print("0");
          Serial.print(v3, HEX);
          Serial.println("]");

          // Accept only stable 0x33
          if (v1 == 0x33 && v2 == 0x33 && v3 == 0x33) {
            hitCount++;
            Serial.println(">>> MATCH FOUND <<<");
            printOneResult(pinCS, pinSCK, pinMOSI, pinMISO, v1);
            Serial.println();
          }
        }
      }
    }
  }

  Serial.println();
  Serial.print("Search complete. Stable matches found: ");
  Serial.println(hitCount);

  if (hitCount == 0) {
    Serial.println("No valid LIS3DH mapping found on pins 33/34/35/36.");
  }
}

void loop() {
  // nothing
}