# V2X-Intelligence: Redefining Emergency Response
## OVERVIEW
This is an intelligent traffic management solution designed to reduce emergency response times. It uses V2I (Vehicle-to-Infrastructure) to preempt traffic lights and V2V (Vehicle-to-Vehicle) to alert surrounding drivers.
## SYSTEM ARCHITECTURE
<img width="876" height="415" alt="image" src="https://github.com/user-attachments/assets/63e01652-4d0c-483e-930f-b7aaa67a41f1" />  


The architecture is split into three main nodes, each handling specific communication pathways:

### 1. Host Vehicle (Emergency Transmitter)
The emergency vehicle acts as the primary data transmitter.
* **Processing:** On-board **Raspberry Pi 4B**.
* **Data:** Captures real-time telemetry via a **GPS Antenna**.
* **Broadcasting:** Packages "uncooked" spatial info for simultaneous V2I and V2V transmission.

### 2. Traffic Junction RSU (V2I Pathway)
Automates intersection clearing by overriding standard traffic timers.
* **Communication:** Subscribes to a **Cloud-based MQTT Broker** (HiveMQ) via a cellular connection.
* **Decision Engine:** Analyzes vehicle distance using **Geofencing Logic** (implemented via Python `Shapely`).
* **Hardware Override:** Sends serial commands to an **Arduino Uno** to trigger physical green lights upon vehicle entry into the active sector.

### 3. Remote Vehicle (V2V Pathway)
Ensures localized awareness for nearby civilian drivers.
* **Protocol:** Peer-to-peer proximity warnings using **UDP over Wi-Fi Direct** (simulating PC5 Sidelink technology).
* **Processing:** Analyzes if the vehicle is in the host's direct path.
* **Alerting:** Immediate **Audio-Visual alerts** integrated into the car’s infotainment/display system.


## Tech Stack
**Hardware**:Raspberry Pi 4b, Neo 6M GPS Module, Waveshare 7600-H Dongle, ARDUINO UNO, 5V Traffic Light Module *5, 12V Power Supply
**Software**: Google My Maps, Graphhopper, Open Street Map API, Google Map API, Ubuntu OS  
**Languages:** Python 3.x, C++ (Arduino)  
**Cloud Protocol:** MQTT (via `paho-mqtt`)  
**Local Protocol:** UDP / Wi-Fi Direct

#  Execution Guide:

Follow this specific sequence to initialize the V2X network for a live demonstration.

---

## 1. Host Vehicle (The Transmitter)
*The ambulance must start broadcasting telemetry to the cloud and local network first.*

1.  **Hardware Connection:** Connect the **GPS Antenna** to the Raspberry Pi 4B.
2.  **Internet Access:** Ensure the Pi is connected to the internet (via 4G Hat or Hotspot) to reach the MQTT Broker.
3.  **Run Broadcasting Script:**
    ```bash
    integrator.py
    ```


---

## 2. Remote Vehicle (The Civilian Alert)
*The civilian node is set to 'Passive Listening' mode to wait for the host's signal.*

1.  **Network Setup:** Connect the unit to the same localized Wi-Fi Direct network or subnet as the Host Vehicle.
2.  **Run Alert Script:**
    ```bash
    python remote_receiver.py
    ```


---

## 3. Traffic Junction RSU (The Controller)
*The infrastructure unit is the final piece that manages the physical hardware override.*

1.  **Hardware Connection:** Plug the **Arduino Uno** into the laptop via USB.
2.  **Arduino Setup:** Upload `junction_logic.ino` using the Arduino IDE. Confirm the Port (e.g., `COM5`).
3.  **Run Decision Engine:**
    ```bash
    v2xrsutest.py
    ```
4.  **Success Indicator:** * The console should print `Connected to Arduino on COM5`.
    * A **Google Maps** window will launch, showing the geofenced sectors.
    * The "Host Vehicle" marker should appear on the map as soon as the MQTT stream is received.
