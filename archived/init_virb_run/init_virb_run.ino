int pwm1 = 2;
int dir1 = 3;
int pwm2 = 4;
int dir2 = 5;
int pwm3 = 6;
void setup() {
  pinMode(pwm1, OUTPUT);
  pinMode(dir1, OUTPUT);
  
  digitalWrite(dir1, HIGH);  
  digitalWrite(dir2, HIGH);  
  analogWriteFrequency(pwm1, 300);
  analogWriteFrequency(pwm2, 240);
  analogWriteFrequency(pwm3, 240);
}

void loop() {
  //analogWrite(pwm1, 127); 
  analogWrite(pwm1, 80); 
  delay(100);
  analogWrite(pwm2, 80); 
  delay(100);
  analogWrite(pwm3, 127); 
  delay(100);

  //analogWrite(pwm1, 150); 
  //delay(1000);

  analogWrite(pwm1, 0); 
  analogWrite(pwm2, 0); 
  analogWrite(pwm3, 0); 
  delay(5000);
}