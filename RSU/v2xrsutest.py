import webview
import serial
import os
import time
import json
import xml.etree.ElementTree as ET
from shapely.geometry import Point, Polygon
import paho.mqtt.client as mqtt
import threading
import queue  # <-- ADDED for asynchronous Arduino communication

# --- CONFIGURATION ---
RSU_LAT = 8.5188035
RSU_LON = 76.9422819
ARDUINO_PORT = 'COM5' 
BAUD_RATE = 9600
KML_FILE = r"C:\Users\surya\Downloads\UST Map (1).kml"

# --- PASTE YOUR GOOGLE MAPS API KEY HERE ---
GOOGLE_API_KEY = "PASTE YOUR GOOGLE MAPS API KEY HERE" 

# --- ARDUINO CONNECTION ---
try:
    arduino = serial.Serial(port=ARDUINO_PORT, baudrate=BAUD_RATE, timeout=0.1)
    print(f"Connected to Arduino on {ARDUINO_PORT}")
except:
    arduino = None
    print("Arduino not connected. Simulation Mode.")

# --- ARDUINO COMMAND QUEUE (NEW) ---
arduino_queue = queue.Queue()

def arduino_worker():
    """Background thread that handles Arduino writes so MQTT never blocks"""
    while True:
        signal_code = arduino_queue.get()
        if arduino:
            try:
                arduino.write(bytes(signal_code + '\n', 'utf-8'))
                time.sleep(0.1) 
                # If we sent a green light, reset to stop immediately after
                if signal_code.startswith("TL"):
                    arduino.write(bytes('STOP\n', 'utf-8'))
            except Exception as e:
                print(f"Arduino write error: {e}")
        arduino_queue.task_done()

# Start the Arduino worker thread immediately
threading.Thread(target=arduino_worker, daemon=True).start()

# --- MQTT TIMEOUT TRACKING ---
last_mqtt_time = None
mqtt_timeout = 5  # seconds

def mqtt_watchdog():
    """Monitor if MQTT stops sending. After 5 seconds of no messages, send STOP to Arduino."""
    global last_mqtt_time
    while True:
        time.sleep(1)  # Check every second
        if last_mqtt_time is not None:
            elapsed = time.time() - last_mqtt_time
            if elapsed > mqtt_timeout:
                print(f"⏱ MQTT timeout detected ({elapsed:.1f}s). Sending STOP to Arduino.")
                arduino_queue.put("STOP")
                last_mqtt_time = None  # Reset to avoid repeated STOP commands

# Start MQTT watchdog thread
threading.Thread(target=mqtt_watchdog, daemon=True).start()


class TrafficController:
    def _init_(self):
        self.zones = {}
        self.locked_location = None  # Store the locked UST location
        self.last_mqtt_state = {}  # Track latest MQTT coordinates and results
        self.load_kml_zones_robust()

    def load_kml_zones_robust(self):
        """ Robust XML Parser for KML Polygons """
        if not os.path.exists(KML_FILE):
            print(f"CRITICAL ERROR: File not found at: {KML_FILE}")
            return

        print(f"Reading KML file from: {KML_FILE}")
        try:
            tree = ET.parse(KML_FILE)
            root = tree.getroot()
            found_count = 0
            
            for elem in root.iter():
                if 'Placemark' in elem.tag:
                    name = "UNKNOWN"
                    polygon_coords = None
                    
                    for child in elem:
                        if 'name' in child.tag:
                            name = child.text.strip()
                    
                    for sub in elem.iter():
                        if 'Polygon' in sub.tag:
                            for coord_node in sub.iter():
                                if 'coordinates' in coord_node.tag:
                                    polygon_coords = coord_node.text.strip()
                                    break
                    
                    if polygon_coords:
                        points = []
                        for pair in polygon_coords.split():
                            parts = pair.split(',')
                            lon = float(parts[0])
                            lat = float(parts[1])
                            points.append((lon, lat))
                        
                        self.zones[name.upper()] = Polygon(points)
                        print(f" -> Loaded Zone: {name}")
                        found_count += 1
            
            if found_count == 0:
                print("\nWARNING: No Polygons found! Did you draw lines instead of shapes?")

        except Exception as e:
            print(f"XML Parsing Error: {e}")

    # --- FUNCTION TO SEND ZONES TO JAVASCRIPT ---
    def get_zones_for_drawing(self):
        """ Returns the zone coordinates so Google Maps can draw them """
        zones_data = []
        for name, poly in self.zones.items():
            # Convert to Google Maps format {lat: y, lng: x}
            path = [{"lat": y, "lng": x} for x, y in poly.exterior.coords]
            
            # Color logic
            color = "#FF0000"
            if "UST SECTOR" in name: color = "#00FF00" # Green
            elif "MAIN GATE" in name: color = "#FF0000" # Red
            elif "MLCP" in name: color = "#0000FF" # Blue
            elif "GROUND" in name: color = "#FFFF00" # Yellow

            zones_data.append({"name": name, "color": color, "path": path})
        return zones_data

    def process_click(self, lat, lng):
        print(f"\nClick at: {lat}, {lng}")
        click_point = Point(lng, lat) 
        
        # STEP 1: If location not locked, try to lock UST location
        if self.locked_location is None:
            is_in_ust = False
            if "UST SECTOR" in self.zones:
                if self.zones["UST SECTOR"].contains(click_point):
                    is_in_ust = True
            
            if not is_in_ust:
                print("Click rejected: Not inside UST sector")
                return {"status": "error", "message": "ID:KL01CW6590 not inside this sector"}
            
            # Lock this location
            self.locked_location = (lat, lng)
            print(f"Location LOCKED in UST SECTOR at: {lat}, {lng}")
            return {"status": "locked", "message": "Location locked in UST. Click to select junction."}
        
        # STEP 2: Location is locked, now detect which junction
        detected_zone = "NONE"
        signal_code = "STOP"
        
        for zone_name, polygon in self.zones.items():
            if zone_name != "UST SECTOR" and polygon.contains(click_point):  # Ignore UST itself
                detected_zone = zone_name
                break
        
        # If clicked in empty area (no zone detected), reset the process
        if detected_zone == "NONE":
            self.locked_location = None
            print("Clicked in empty zone. Resetting process for new UST lock.")
            return {"status": "reset", "message": "No junction detected. Process reset. Click UST to lock new location."}
        
        # Valid junction detected - send signals but continue accepting more junctions
        if "MAIN GATE" in detected_zone: signal_code = "TL1:G"
        elif "MLCP" in detected_zone: signal_code = "TL2:G"
        elif "GROUND" in detected_zone: signal_code = "TL3:G"
        
        print(f"Detected Junction: {detected_zone} -> Sending: {signal_code}")
        
        # Instantly put in queue instead of waiting for serial write
        arduino_queue.put(signal_code)

    def detect_lane_approach(self, host_lat, host_lng):
        """Detect which lane the vehicle is approaching based on proximity to zones."""
        host_point = Point(host_lng, host_lat)
        locked_point = Point(self.locked_location[1], self.locked_location[0]) if self.locked_location else None
        
        closest_zone = None
        closest_distance = float('inf')
        
        # Check which junction zone the vehicle is closest to
        for zone_name, polygon in self.zones.items():
            if zone_name == "UST SECTOR":
                continue
            
            # Calculate distance from host to polygon boundary
            distance = host_point.distance(polygon.exterior)
            
            if distance < closest_distance:
                closest_distance = distance
                closest_zone = zone_name
        
        # If within ~100m (0.001 lat/lng degrees ≈ 111m), consider it approaching
        if closest_distance < 0.001 and closest_zone:
            return closest_zone
        
        return "NONE"

    def get_nearest_lane_distance(self, host_lat, host_lng):
        """Get distance to nearest lane in degrees."""
        host_point = Point(host_lng, host_lat)
        min_distance = float('inf')
        
        for zone_name, polygon in self.zones.items():
            if zone_name == "UST SECTOR":
                continue
            distance = host_point.distance(polygon.exterior)
            min_distance = min(min_distance, distance)
        
        return min_distance

    def process_mqtt(self, junction_lat, junction_lng, host_lat, host_lng):
        """Process MQTT coordinates: use junction for lock, host for junction detection."""
        print(f"\nMQTT: Junction({junction_lat}, {junction_lng}) Host({host_lat}, {host_lng})")
        
        # Track latest MQTT state for map display
        self.last_mqtt_state = {
            "junction": {"lat": junction_lat, "lng": junction_lng},
            "host": {"lat": host_lat, "lng": host_lng},
            "locked": self.locked_location is not None,
            "detected_zone": "NONE",
            "status": "processing"
        }
        
        junction_point = Point(junction_lng, junction_lat)
        host_point = Point(host_lng, host_lat)
        
        # STEP 1: Junction Lock Phase
        if self.locked_location is None:
            is_in_ust = False
            if "UST SECTOR" in self.zones:
                if self.zones["UST SECTOR"].contains(junction_point):
                    is_in_ust = True
            
            if not is_in_ust:
                print("MQTT rejected: Point not inside UST sector")
                self.last_mqtt_state["status"] = "error"
                return {"status": "error", "message": "Point not in UST sector"}
            
            self.locked_location = (junction_lat, junction_lng)
            print(f"Location LOCKED via MQTT in UST SECTOR at: {junction_lat}, {junction_lng}")
            self.last_mqtt_state["locked"] = True
            self.last_mqtt_state["status"] = "locked"
            return {"status": "locked", "message": "Location locked in UST via MQTT."}
        
        # STEP 2: Lane Detection (only when locked) - using proximity-based approach
        detected_zone = "NONE"
        signal_code = "STOP"
        
        # First check if host point is directly in a junction zone
        for zone_name, polygon in self.zones.items():
            if zone_name != "UST SECTOR" and polygon.contains(host_point):
                detected_zone = zone_name
                break
        
        # If not directly in zone, check which lane they're approaching
        if detected_zone == "NONE":
            detected_zone = self.detect_lane_approach(host_lat, host_lng)
        
        self.last_mqtt_state["detected_zone"] = detected_zone
        self.last_mqtt_state["host_plotted"] = True
        
        # Lane Signal Mapping
        if detected_zone != "NONE":
            if "MAIN GATE" in detected_zone: signal_code = "TL1:G"
            elif "MLCP" in detected_zone: signal_code = "TL2:G"
            elif "GROUND" in detected_zone: signal_code = "TL3:G"
            
            print(f"Detected Lane via MQTT: {detected_zone} -> Sending: {signal_code}")
            
            # Instantly put in queue. No sleeping!
            arduino_queue.put(signal_code)
            
            self.last_mqtt_state["status"] = "success"
            return {"status": "success", "message": f"Lane Detected: {detected_zone}"}
        else:
            # Vehicle in transit, keep lock stable
            print(f"Vehicle in transit, lock maintained. Distance to nearest lane: {self.get_nearest_lane_distance(host_lat, host_lng):.4f}°")
            self.last_mqtt_state["status"] = "transit"
            return {"status": "transit", "message": "Vehicle in transit, lock maintained."}
    
    def get_mqtt_state(self):
        """Return the latest MQTT state for map visualization."""
        return self.last_mqtt_state


# --- MAIN APP ---
if _name_ == '_main_':
    api = TrafficController()
    
    # --- MQTT LISTENER SETUP ---
    TOPIC = "v2x_project/4g_data_stream"
    BROKER = "broker.hivemq.com"
    PORT = 1883
    
    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"✓ Connected to Cloud Broker! Listening on: {TOPIC}")
            client.subscribe(TOPIC)
            print(f"✓ Successfully subscribed to {TOPIC}")
        else:
            error_codes = {
                1: "Connection refused - incorrect protocol version",
                2: "Connection refused - invalid client identifier",
                3: "Connection refused - server unavailable",
                4: "Connection refused - bad username or password",
                5: "Connection refused - not authorised",
            }
            error_msg = error_codes.get(rc, f"Unknown error code {rc}")
            print(f"✗ Failed to connect, return code {rc}: {error_msg}")

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
        if reason_code == 0:
            print(f"✓ Gracefully disconnected from broker")
        else:
            print(f"✗ Unexpected disconnection from broker (code: {reason_code})")

    def on_message(client, userdata, msg):
        global last_mqtt_time
        last_mqtt_time = time.time()  # Update timestamp on every message
        
        payload = msg.payload.decode()
        print(f"Received MQTT payload: {payload}")
        
        try:
            # Try JSON format first: {"junction":[lat,lng],"host":[lat,lng]}
            data = json.loads(payload)
            j_lat, j_lng = data["junction"]
            h_lat, h_lng = data["host"]
        except Exception:
            try:
                # Try semicolon-separated format: jlat,jlng;hlat,hlng
                parts = payload.split(";")
                j_parts = parts[0].split(",")
                h_parts = parts[1].split(",")
                j_lat, j_lng = float(j_parts[0]), float(j_parts[1])
                h_lat, h_lng = float(h_parts[0]), float(h_parts[1])
            except Exception:
                try:
                    # Try pipe-separated format: Vehicle:(lat,lng)|Target:(lat,lng)|...
                    import re
                    vehicle_match = re.search(r'Vehicle:\(([0-9.]+),([0-9.]+)\)', payload)
                    junction_match = re.search(r'Target:\(([0-9.]+),([0-9.]+)\)', payload)
                    
                    if vehicle_match and junction_match:
                        h_lat, h_lng = float(vehicle_match.group(1)), float(vehicle_match.group(2))
                        j_lat, j_lng = float(junction_match.group(1)), float(junction_match.group(2))
                    else:
                        print(f"Could not extract coordinates from payload")
                        return
                except Exception as e:
                    print(f"Malformed MQTT message, cannot parse coordinates: {e}")
                    return
        
        result = api.process_mqtt(j_lat, j_lng, h_lat, h_lng)
        print(f"MQTT processing result: {result}")
    
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message = on_message
    
    def start_mqtt():
        """Start MQTT connection in background thread"""
        try:
            print(f"[MQTT] Attempting fast async connection to {BROKER}:{PORT}...")
            # connect_async is non-blocking and returns instantly
            mqtt_client.connect_async(BROKER, PORT, keepalive=60)
            mqtt_client.loop_start()
            print("[MQTT] Network loop started. Ready to receive streams.")
        except Exception as e:
            print(f"✗ Failed to start MQTT listener: {e}")
            import traceback
            traceback.print_exc()
    
    # Start MQTT in background thread so webview doesn't wait
    mqtt_thread = threading.Thread(target=start_mqtt, daemon=True)
    mqtt_thread.start()
    print("[MQTT] Background thread started - webview launching immediately\n")
    
    # --- GOOGLE MAPS HTML ---
    html_content = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <style>html, body, #map {{ height: 100%; margin: 0; }}</style>
        
        <script src="https://maps.googleapis.com/maps/api/js?key={GOOGLE_API_KEY}&callback=initMap" async defer></script>
        
        <script>
          let map;
          let clickMarker;
          let laneMarker;
          let junctionMarker;
          let hostMarker;
          let mqttInfoWindow;
          let locationLocked = false;

          function initMap() {{
            const rsu = {{ lat: {RSU_LAT}, lng: {RSU_LON} }};
            
            map = new google.maps.Map(document.getElementById("map"), {{
              zoom: 17,
              center: rsu,
              mapTypeId: "hybrid", // Satellite View
              disableDefaultUI: false
            }});

            // 1. ASK PYTHON FOR ZONES AND DRAW THEM
            window.addEventListener('pywebviewready', function() {{
                window.pywebview.api.get_zones_for_drawing().then(zones => {{
                    drawZones(zones);
                }});
                // Start polling MQTT state for markers
                setInterval(updateMQTTMarkers, 500);
            }});

            // 2. CLICK LISTENER
            map.addListener("click", (e) => {{
              window.pywebview.api.process_click(e.latLng.lat(), e.latLng.lng()).then(response => {{
                if (response.status === "error") {{
                  alert(response.message);
                }} else if (response.status === "locked") {{
                  if (clickMarker) clickMarker.setMap(null);
                  
                  clickMarker = new google.maps.Marker({{
                    position: e.latLng,
                    map: map,
                    title: "Location Locked (UST)",
                    icon: "http://maps.google.com/mapfiles/ms/icons/red-dot.png"
                  }});
                  
                  locationLocked = true;
                  alert(response.message);
                }} else if (response.status === "success") {{
                  if (laneMarker) laneMarker.setMap(null);
                  
                  laneMarker = new google.maps.Marker({{
                    position: e.latLng,
                    map: map,
                    title: "Junction Position",
                    icon: "http://maps.google.com/mapfiles/ms/icons/yellow-dot.png"
                  }});
                  
                  alert(response.message);
                }} else if (response.status === "reset") {{
                  if (clickMarker) clickMarker.setMap(null);
                  if (laneMarker) laneMarker.setMap(null);
                  locationLocked = false;
                  alert(response.message);
                }}
              }});
            }});
          }}

          function updateMQTTMarkers() {{
            window.pywebview.api.get_mqtt_state().then(state => {{
              if (!state || !state.junction) return;
              
              const junctionPos = {{ lat: state.junction.lat, lng: state.junction.lng }};
              if (!junctionMarker) {{
                junctionMarker = new google.maps.Marker({{
                  position: junctionPos,
                  map: map,
                  title: "Junction (via MQTT)",
                  icon: "http://maps.google.com/mapfiles/ms/icons/blue-dot.png"
                }});
              }} else {{
                junctionMarker.setPosition(junctionPos);
              }}
              
              if (state.locked && state.host) {{
                const hostPos = {{ lat: state.host.lat, lng: state.host.lng }};
                if (!hostMarker) {{
                  hostMarker = new google.maps.Marker({{
                    position: hostPos,
                    map: map,
                    title: "Vehicle Position (via MQTT)",
                    icon: "http://maps.google.com/mapfiles/ms/icons/yellow-dot.png"
                  }});
                }} else {{
                  hostMarker.setPosition(hostPos);
                }}
                
                if (state.detected_zone && state.detected_zone !== "NONE") {{
                  if (mqttInfoWindow) mqttInfoWindow.close();
                  mqttInfoWindow = new google.maps.InfoWindow({{
                    content: "<strong>Lane Detected:</strong> " + state.detected_zone + "<br/><strong>Status:</strong> " + state.status,
                    position: hostPos
                  }});
                  mqttInfoWindow.open(map);
                }} else if (state.status === "transit") {{
                  if (mqttInfoWindow) mqttInfoWindow.close();
                  mqttInfoWindow = new google.maps.InfoWindow({{
                    content: "<strong>Status:</strong> Vehicle in Transit<br/><strong>Lock:</strong> Maintained",
                    position: hostPos
                  }});
                  mqttInfoWindow.open(map);
                }}
              }} else {{
                if (hostMarker) hostMarker.setMap(null);
                if (mqttInfoWindow) mqttInfoWindow.close();
              }}
            }}).catch(err => {{
            }});
          }}

          function drawZones(zones) {{
            zones.forEach(zone => {{
                console.log("Drawing:", zone.name);
                const poly = new google.maps.Polygon({{
                    paths: zone.path,
                    strokeColor: zone.color,
                    strokeOpacity: 0.8,
                    strokeWeight: 2,
                    fillColor: zone.color,
                    fillOpacity: 0.35,
                    map: map
                }});
                
                poly.addListener("click", (e) => {{
                    google.maps.event.trigger(map, 'click', e);
                }});
            }});
          }}
        </script>
      </head>
      <body>
        <div id="map"></div>
      </body>
    </html>
    """

    window = webview.create_window('Google Maps Traffic Controller', html=html_content, js_api=api)
    webview.start()
