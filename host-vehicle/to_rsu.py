import paho.mqtt.client as mqtt
import time
import math
import uuid  # For generating random MQTT client IDs

# Configuration
MQTT_BROKER = "broker.hivemq.com"  
MQTT_PORT = 1883
MQTT_TOPIC = "v2x_project/4g_data_stream"
BROADCAST_THRESHOLD_SECONDS = 60  

# Global State
mqtt_client = None
mqtt_connected = False  
tracking_active = False 

def on_connect(client, userdata, flags, rc):
    """MQTT connection callback"""
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        print(f"[MQTT] ✓ Connected successfully to {MQTT_BROKER}:{MQTT_PORT}")
    else:
        mqtt_connected = False
        print(f"[MQTT] ✗ Connection failed with code {rc}")

def on_disconnect(client, userdata, rc):
    """MQTT disconnection callback"""
    global mqtt_connected
    mqtt_connected = False
    print(f"[MQTT] Disconnected from broker")

def init_mqtt():
    """Initialize MQTT client - Connect to HiveMQ Cloud Broker"""
    global mqtt_client, mqtt_connected
    try:
        print(f"[MQTT] Connecting to {MQTT_BROKER}:{MQTT_PORT}...")
        
        # Generate a random UUID so Flask threads never collide
        random_id = f"v2x_amb_{uuid.uuid4().hex[:8]}" 
        mqtt_client = mqtt.Client(client_id=random_id) 
        
        mqtt_client.on_connect = on_connect
        mqtt_client.on_disconnect = on_disconnect
        
        # Enable automatic reconnect
        mqtt_client.reconnect_delay_set(min_delay=1, max_delay=32)
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        mqtt_client.loop_start()
        
        # Wait till 5 seconds for connection
        for _ in range(50):
            if mqtt_connected:
                return True
            time.sleep(0.1)
            
        print("[MQTT] ⚠ Connection timeout - will retry automatically")
        return True 
    except Exception as e:
        print(f"[MQTT] ✗ Initialization error: {e}")
        return False

def parse_eta_to_seconds(eta_str):
    """
    Converts Google's text ETA (e.g., '2 mins', '1 hour 5 mins') into raw seconds.
    """
    if not eta_str or eta_str in ["Calculating...", "N/A"]:
        return 999999  # Return a massive number so it doesn't accidentally trigger the broadcast
        
    seconds = 0
    parts = eta_str.split()
    
    try:
        for i in range(len(parts)):
            if 'hour' in parts[i]:
                seconds += int(parts[i-1]) * 3600
            elif 'min' in parts[i]:
                seconds += int(parts[i-1]) * 60
            elif 'sec' in parts[i]:
                seconds += int(parts[i-1])
        return seconds
    except Exception as e:
        print(f"[MQTT] Error parsing ETA string '{eta_str}': {e}")
        return 999999

def set_junctions(junctions):
    pass 

def update_vehicle_position(latitude, longitude, target_junctions=None):
    """
    Called by Flask on every GPS update. 
    Checks the real Google Traffic ETA threshold and publishes if close.
    """
    global tracking_active, mqtt_client, mqtt_connected
    
    if not tracking_active or not target_junctions or not mqtt_client:
        return

    target = target_junctions[0]
    
    google_eta_text = target.get('eta_duration', 'Calculating...')
    
    eta_seconds = parse_eta_to_seconds(google_eta_text)

    if eta_seconds <= BROADCAST_THRESHOLD_SECONDS:
        
        # We are close! Start broadcasting to OMNeT++
        try:
            message = f"Vehicle:({latitude:.6f},{longitude:.6f})|Target:({target['latitude']:.6f},{target['longitude']:.6f})|ETA:{google_eta_text}|Location:{target.get('name', 'Unknown')}"
            
            result = mqtt_client.publish(MQTT_TOPIC, message)
            
            if result.rc == 0:
                print(f"[MQTT] 📡 BROADCASTING: {target.get('name', 'Unknown')} (Traffic ETA: {google_eta_text})")
        except Exception as e:
            print(f"[MQTT] ✗ Publish error: {e}")
            
    else:
        # We are too far away. Stay completely silent on the MQTT channel.
        # This keeps your OMNeT++ simulation clean of unnecessary noise.
        print(f"[MQTT] 🔇 Silent (ETA: {google_eta_text} > Threshold: {BROADCAST_THRESHOLD_SECONDS}s)")

def start_tracking():
    global tracking_active
    tracking_active = True
    print("[System] MQTT broadcast activated. Waiting for host coordinates...")

def stop_tracking():
    global tracking_active
    tracking_active = False
    print("[MQTT] Kill signal received. Halting all broadcasts.")

def cleanup():
    global mqtt_client, mqtt_connected
    if mqtt_client:
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except:
            pass
        mqtt_connected = False
