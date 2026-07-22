const int PIN_CS   = 36;
const int PIN_SCK  = 33;
const int PIN_MOSI = 34;
const int PIN_MISO = 35;

void spiDelay() {
  delayMicroseconds(3);
}

// SPI MODE3
uint8_t spiTransfer(uint8_t data) {
  uint8_t rx = 0;

  for (int i = 7; i >= 0; i--) {

    digitalWrite(PIN_SCK, HIGH);
    spiDelay();

    digitalWrite(PIN_MOSI, (data >> i) & 1);
    spiDelay();

    digitalWrite(PIN_SCK, LOW);
    spiDelay();

    rx <<= 1;
    if (digitalRead(PIN_MISO))
      rx |= 1;

    spiDelay();
  }

  digitalWrite(PIN_SCK, HIGH);
  return rx;
}

void writeReg(uint8_t reg, uint8_t val) {
  digitalWrite(PIN_CS, LOW);
  spiTransfer(reg & 0x7F);
  spiTransfer(val);
  digitalWrite(PIN_CS, HIGH);
}

uint8_t readReg(uint8_t reg) {
  digitalWrite(PIN_CS, LOW);
  spiTransfer(0x80 | reg);
  uint8_t v = spiTransfer(0x00);
  digitalWrite(PIN_CS, HIGH);
  return v;
}

void readAccel(int16_t &x, int16_t &y, int16_t &z) {

  digitalWrite(PIN_CS, LOW);

  spiTransfer(0xC0 | 0x28);   // read + auto increment

  uint8_t xL = spiTransfer(0);
  uint8_t xH = spiTransfer(0);
  uint8_t yL = spiTransfer(0);
  uint8_t yH = spiTransfer(0);
  uint8_t zL = spiTransfer(0);
  uint8_t zH = spiTransfer(0);

  digitalWrite(PIN_CS, HIGH);

  x = (int16_t)((xH << 8) | xL) >> 4;
  y = (int16_t)((yH << 8) | yL) >> 4;
  z = (int16_t)((zH << 8) | zL) >> 4;
}

void setup() {

  Serial.begin(115200);

  pinMode(PIN_CS, OUTPUT);
  pinMode(PIN_SCK, OUTPUT);
  pinMode(PIN_MOSI, OUTPUT);
  pinMode(PIN_MISO, INPUT);

  digitalWrite(PIN_CS, HIGH);
  digitalWrite(PIN_SCK, HIGH);

  delay(100);

  Serial.println("LIS3DH init");

  // 开启传感器
  writeReg(0x20, 0x77);   // 400Hz + XYZ enable
  writeReg(0x23, 0x88);   // High resolution

}

void loop() {

  int16_t x,y,z;

  readAccel(x,y,z);

  Serial.print(x);
  Serial.print(",");
  Serial.print(y);
  Serial.print(",");
  Serial.println(z);

  delay(10);
}