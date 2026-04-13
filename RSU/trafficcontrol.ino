// ==========================================
// CONFIGURATION: PIN MAPPING
// ==========================================
// ROAD 1 (NORTH)
const int R1 = 2; const int Y1 = 3; const int G1 = 4;
// ROAD 2 (SOUTH)
const int R2 = 5; const int Y2 = 6; const int G2 = 7;
// ROAD 3 (EAST)
const int R3 = 8; const int Y3 = 9; const int G3 = 10;
// ROAD 4 (WEST)
const int R4 = 11; const int Y4 = 12; const int G4 = 13;

// TIMING SETTINGS (Normal Mode)
const long GREEN_TIME = 6000;
const long YELLOW_TIME = 2000;

// TIMING SETTINGS (Interrupt Blink & Delay)
const long BLINK_SPEED = 500; // Yellow lights blink every 500ms
const long STOP_DELAY_TIME = 9000; // 5 seconds delay before returning to normal

// ==========================================
// SYSTEM VARIABLES
// ==========================================
unsigned long previousMillis = 0; 
int cycleState = 0;              

// INTERRUPT VARIABLES
bool isInterrupted = false;       
int overrideTarget = 0;           // 0=None, 1=North, 2=South, 3=East, 4=West
unsigned long blinkMillis = 0;    // Timer for the blinking effect
bool blinkState = LOW;            // Current state of the blinking light (High/Low)

// NEW: STOP DELAY VARIABLES
bool stopRequested = false;       // Tracks if "STOP" has been received
unsigned long stopDelayMillis = 0;// Timer for the 5-second stop delay

void setup() {
  Serial.begin(9600);
  for (int i = 2; i <= 13; i++) {
    pinMode(i, OUTPUT);
    digitalWrite(i, LOW);
  }
}

void loop() {
  // ==================================================
  // 1. CHECK FOR COMMANDS
  // ==================================================
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();

    if (command == "STOP") {
      // Only trigger the stop delay if we are currently interrupted
      if (isInterrupted && !stopRequested) {
        stopRequested = true; 
        stopDelayMillis = millis(); // Start the 5-second countdown
      }
    } 
    else {
      isInterrupted = true; 
      stopRequested = false; // Cancel any pending stop if a new emergency comes in
      
      // Identify which road needs to be GREEN
      if (command == "TL1:G") overrideTarget = 1; // North
      else if (command == "TL2:G") overrideTarget = 2; // South
      else if (command == "TL3:G") overrideTarget = 3; // East
      else if (command == "TL4:G") overrideTarget = 4; // West
    }
  }

  // ==================================================
  // 2. RUN LOGIC BASED ON MODE
  // ==================================================
  if (isInterrupted) {
    // Check if a STOP was requested AND 5 seconds have passed
    if (stopRequested && (millis() - stopDelayMillis >= STOP_DELAY_TIME)) {
      isInterrupted = false;    // Revert to normal mode
      stopRequested = false;    // Reset the stop flag
      previousMillis = millis();// Reset normal timer so we don't skip states
      updateLights(cycleState); // Refresh normal lights immediately
    } else {
      // Run the Blinking Yellow + Forced Red/Green Logic
      // This will continue running during the 5-second delay
      runOverrideBlinkAnimation();
    }
  } 
  else {
    // Run Standard Cycle
    runNormalTrafficCycle();
  }
}

// --------------------------------------------------
// OVERRIDE WITH BLINKING YELLOWS
// --------------------------------------------------
void runOverrideBlinkAnimation() {
  unsigned long currentMillis = millis();

  // 1. Handle the Blinking Timer (Non-blocking)
  if (currentMillis - blinkMillis >= BLINK_SPEED) {
    blinkMillis = currentMillis;
    blinkState = !blinkState; // Toggle On/Off
  }

  // 2. Determine which Red/Green lights should be ON
  int northGreen = (overrideTarget == 1) ? HIGH : LOW;
  int northRed   = (overrideTarget == 1) ? LOW  : HIGH;
  
  int southGreen = (overrideTarget == 2) ? HIGH : LOW;
  int southRed   = (overrideTarget == 2) ? LOW  : HIGH;

  int eastGreen  = (overrideTarget == 3) ? HIGH : LOW;
  int eastRed    = (overrideTarget == 3) ? LOW  : HIGH;

  int westGreen  = (overrideTarget == 4) ? HIGH : LOW;
  int westRed    = (overrideTarget == 4) ? LOW  : HIGH;

  // 3. Update the Pins
  
  // NORTH
  digitalWrite(G1, northGreen);
  digitalWrite(R1, northRed);
  digitalWrite(Y1, blinkState); 

  // SOUTH
  digitalWrite(G2, southGreen);
  digitalWrite(R2, southRed);
  digitalWrite(Y2, blinkState);

  // EAST
  digitalWrite(G3, eastGreen);
  digitalWrite(R3, eastRed);
  digitalWrite(Y3, blinkState);

  // WEST
  digitalWrite(G4, westGreen);
  digitalWrite(R4, westRed);
  digitalWrite(Y4, blinkState);
}

// --------------------------------------------------
// NORMAL TRAFFIC CYCLE
// --------------------------------------------------
void runNormalTrafficCycle() {
  unsigned long currentMillis = millis();
  long currentInterval = (cycleState % 2 == 0) ? GREEN_TIME : YELLOW_TIME;

  if (currentMillis - previousMillis >= currentInterval) {
    previousMillis = currentMillis; 
    cycleState++;
    if (cycleState > 7) cycleState = 0; 
    updateLights(cycleState);
  }
}

// --------------------------------------------------
// HELPER: Map Cycle State to Actual Lights
// --------------------------------------------------
void updateLights(int state) {
  allLightsOff();
  switch (state) {
    case 0: digitalWrite(G1, HIGH); digitalWrite(R2, HIGH); digitalWrite(R3, HIGH); digitalWrite(R4, HIGH); break;
    case 1: digitalWrite(Y1, HIGH); digitalWrite(R2, HIGH); digitalWrite(R3, HIGH); digitalWrite(R4, HIGH); break;
    case 2: digitalWrite(R1, HIGH); digitalWrite(G2, HIGH); digitalWrite(R3, HIGH); digitalWrite(R4, HIGH); break;
    case 3: digitalWrite(R1, HIGH); digitalWrite(Y2, HIGH); digitalWrite(R3, HIGH); digitalWrite(R4, HIGH); break;
    case 4: digitalWrite(R1, HIGH); digitalWrite(R2, HIGH); digitalWrite(G3, HIGH); digitalWrite(R4, HIGH); break;
    case 5: digitalWrite(R1, HIGH); digitalWrite(R2, HIGH); digitalWrite(Y3, HIGH); digitalWrite(R4, HIGH); break;
    case 6: digitalWrite(R1, HIGH); digitalWrite(R2, HIGH); digitalWrite(R3, HIGH); digitalWrite(G4, HIGH); break;
    case 7: digitalWrite(R1, HIGH); digitalWrite(R2, HIGH); digitalWrite(R3, HIGH); digitalWrite(Y4, HIGH); break;
  }
}

void allLightsOff() {
  for (int i = 2; i <= 13; i++) {
    digitalWrite(i, LOW);
  }
}
