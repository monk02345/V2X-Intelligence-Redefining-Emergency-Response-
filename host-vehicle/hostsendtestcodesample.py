from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import requests
import os
import time
import json
import socket
import polyline
from shapely.geometry import Point, LineString
from to_rsu import set_junctions, update_vehicle_position, start_tracking, init_mqtt, stop_tracking

# Import the gpsd wrapper instead of the custom serial class
import gps

app = Flask(__name__, static_url_path='', static_folder=os.path.dirname(__file__))
CORS(app)

socketio = SocketIO(app, cors_allowed_origins="*")

# ⚠️ SECURITY REMINDER: Revoke this key in Google Cloud and use environment variables later!
GOOGLE_MAPS_API_KEY = "AIzaSyAFmCghEK343LuMuL1DSmCrujAbNcHsi1g"

# GLOBAL VARIABLES 
current_junctions = []
current_route_data = {}
mqtt_initialized = False 
route_linestring = None      # Stores the mathematical wire of the route
last_api_update_time = 0     # Timer for Dynamic Polling

# GPSD INITIALIZATION 
try:
    print("[System] Connecting to local gpsd service...")
    # Connect to the local daemon and tell it to watch for data
    gps_session = gps.gps(mode=gps.WATCH_ENABLE | gps.WATCH_NEWSTYLE)
    print("[System] ✓ gpsd connected successfully.")
except Exception as e:
    print(f"[System] ✗ WARNING: Could not connect to gpsd: {e}")
    gps_session = None

def snap_to_road(lat, lon):
    roads_url = "https://roads.googleapis.com/v1/snapToRoads"
    params = {'path': f"{lat},{lon}", 'interpolate': True, 'key': GOOGLE_MAPS_API_KEY}
    try:
        response = requests.get(roads_url, params=params)
        roads_data = response.json()
        if 'snappedPoints' in roads_data and roads_data['snappedPoints']:
            snapped = roads_data['snappedPoints'][0]['location']
            return snapped['latitude'], snapped['longitude']
        return lat, lon
    except Exception as e:
        print(f"[Roads] Error snapping to road: {e}")
        return lat, lon

def get_gps_coordinates():
    global gps_session
    if not gps_session:
        return None, None
    
    latest_lat = None
    latest_lon = None
    
    try:
        while gps_session.waiting():
            report = gps_session.next()
            
            # 'TPV' is the Time-Position-Velocity report containing coordinates
            if report['class'] == 'TPV':
                mode = getattr(report, 'mode', 1)
                
                # Mode 2 is a 2D fix, Mode 3 is a 3D fix
                if mode >= 2:
                    latest_lat = getattr(report, 'lat', 0.0)
                    latest_lon = getattr(report, 'lon', 0.0)
        
        # If we successfully grabbed coordinates from the buffer
        if latest_lat is not None and latest_lon is not None and latest_lat != 0.0:
            lat_snapped, lon_snapped = snap_to_road(latest_lat, latest_lon)
            return lat_snapped, lon_snapped
        else:
            if int(time.time()) % 5 == 0:
                print("[GPS] Waiting for fix...")
            return None, None
            
    except Exception as e:
        if int(time.time()) % 5 == 0:
            print(f"[GPS] Error reading from daemon: {e}")
        return None, None

@app.route('/')
def index():
    return send_from_directory(os.path.dirname(__file__), 'index.html')

@app.route('/ping')
def ping():
    return jsonify({"status": "pong", "message": "Server is running"})

@app.route('/get_gps')
def get_gps():
    lat, lon = get_gps_coordinates()
    if lat is not None and lon is not None:
        return jsonify({"status": "success", "latitude": lat, "longitude": lon})
    else:
        return jsonify({"status": "error", "message": "Could not acquire GPS fix."}), 202

@app.route('/get_route_junctions', methods=['POST'])
def get_route_junctions():
    global route_linestring
    try:
        data = request.json
        origin = data.get('origin')
        destination = data.get('destination')
        
        if not origin or not destination:
            return jsonify({"error": "Origin and destination are required"}), 400
        
        current_lat, current_lon = get_gps_coordinates()
        current_pos = f"{current_lat},{current_lon}" if current_lat else origin
        
        # GET ROUTE FROM GOOGLE
        directions_url = "https://maps.googleapis.com/maps/api/directions/json"
        params = {'origin': origin, 'destination': destination, 'key': GOOGLE_MAPS_API_KEY, 'alternatives': False}
        response = requests.get(directions_url, params=params)
        directions_data = response.json()
        
        if directions_data['status'] != 'OK':
            return jsonify({"error": f"Directions API error: {directions_data['status']}"}), 400
        
        route = directions_data['routes'][0]
        legs = route['legs']
        
        # Decode the polyline and build a Shapely LineString (using lon, lat for standard X,Y math)
        encoded_polyline = route['overview_polyline']['points']
        decoded_points = polyline.decode(encoded_polyline) 
        route_linestring = LineString([(p[1], p[0]) for p in decoded_points]) 

        junctions = []
        for leg in legs:
            for step in leg['steps']:
                start_location = step['start_location']
                junctions.append({
                    'latitude': start_location['lat'],
                    'longitude': start_location['lng'],
                    'instruction': step['html_instructions']
                })
        
        final_location = legs[-1]['end_location']
        junctions.append({
            'latitude': final_location['lat'], 'longitude': final_location['lng'],
            'instruction': 'Destination'
        })
        
        junctions_with_names = []
        for junction in junctions:
            # Reverse Geocoding
            geocoding_url = "https://maps.googleapis.com/maps/api/geocode/json"
            geo_params = {'latlng': f"{junction['latitude']},{junction['longitude']}", 'key': GOOGLE_MAPS_API_KEY}
            geo_data = requests.get(geocoding_url, params=geo_params).json()
            junction['name'] = geo_data['results'][0].get('formatted_address', 'Unknown') if geo_data['results'] else "Unknown"
            
            # Project the junction's coordinates onto our wire to get its exact distance from the start
            j_point = Point(junction['longitude'], junction['latitude'])
            junction['route_marker'] = route_linestring.project(j_point)
            # Initialize empty ETA fields
            junction['eta_distance'] = 'Calculating...'
            junction['eta_duration'] = 'Calculating...'
            
            junctions_with_names.append(junction)
        
        return jsonify({
            'status': 'success', 'current_position': current_pos,
            'total_distance': route['legs'][0]['distance']['text'],
            'total_duration': route['legs'][0]['duration']['text'],
            'junctions': junctions_with_names
        })
    
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500

def calculate_eta_for_position(current_lat, current_lon):

    global current_junctions, route_linestring, last_api_update_time
    
    if not current_junctions or not route_linestring:
        return []
    
    current_pos = f"{current_lat},{current_lon}"
    car_point = Point(current_lon, current_lat)
    
    # 1. LOCAL MATH: Find car's mile marker
    car_marker = route_linestring.project(car_point)
    
    junctions_to_keep = []
    
    for junction in current_junctions:
        j_marker = junction.get('route_marker', 0)
        
        # --- THE NEW DROP LOGIC ---
        # If the car's marker is greater than the junction's marker (minus a tiny 15-meter tolerance)
        # 0.00015 degrees is roughly 15 meters.
        if car_marker > (j_marker - 0.00015) and junction.get('instruction') != 'Destination':
            print(f"[Tracking] Passed a junction ({junction.get('name', 'Unknown')}). Dropping via Local Math.")
            
            continue 
            
        junctions_to_keep.append(junction)
        
    current_junctions = junctions_to_keep
    if not current_junctions:
        return []

    # 2. DYNAMIC POLLING: How often do we check Google for traffic?
    next_junction = current_junctions[0]
    distance_to_next_deg = abs(next_junction.get('route_marker', 0) - car_marker)
    
    if distance_to_next_deg > 0.009:     # Approx > 1 km
        poll_interval = 20               # Check every 20 seconds
    elif distance_to_next_deg > 0.0045:  # Approx > 500 meters
        poll_interval = 10               # Check every 10 seconds
    else:                                # Very close
        poll_interval = 3                # Check every 3 seconds!

    current_time = time.time()
    
    # Check if it is time to ping the API
    if (current_time - last_api_update_time) >= poll_interval:
        print(f"[API] Updating Traffic ETA. (Polling Interval: {poll_interval}s)")
        
        # Only ping API for the immediate next junction to save massive amounts of credits
        distance_matrix_url = "https://maps.googleapis.com/maps/api/distancematrix/json"
        params = {
            'origins': current_pos,
            'destinations': f"{next_junction['latitude']},{next_junction['longitude']}",
            'departure_time': 'now', # Forces real-time traffic
            'key': GOOGLE_MAPS_API_KEY
        }
        
        try:
            dist_data = requests.get(distance_matrix_url, params=params).json()
            if dist_data['status'] == 'OK' and dist_data['rows']:
                element = dist_data['rows'][0]['elements'][0]
                if element['status'] == 'OK':
                    current_junctions[0]['eta_distance'] = element.get('distance', {}).get('text', 'N/A')
                    current_junctions[0]['eta_duration'] = element.get('duration_in_traffic', element.get('duration', {})).get('text', 'N/A')
        except Exception as e:
            print(f"[API Error] Could not update ETA: {e}")
            
        last_api_update_time = current_time

    return current_junctions

@socketio.on('connect')
def handle_connect():
    emit('connection_response', {'data': 'Connected to real-time ETA server'})

@socketio.on('update_position')
def handle_position_update(data):
    global current_junctions
    try:
        current_lat, current_lon = data.get('latitude'), data.get('longitude')
        if current_lat is None or current_lon is None:
            return
        
        updated_junctions = calculate_eta_for_position(current_lat, current_lon)
        
        try:
            # Feeds the correct data to your RSU script (which handles the < 60s broadcast threshold)
            update_vehicle_position(current_lat, current_lon, updated_junctions)
        except Exception as mqtt_e:
            print(f"[MQTT Error] to_rsu.py failed to send: {str(mqtt_e)}")
        
        eta_response = {
            'current_position': {'latitude': current_lat, 'longitude': current_lon},
            'junctions': updated_junctions,
            'timestamp': time.time()
        }
        emit('eta_update', eta_response, broadcast=True)
        
    except Exception as e:
        print(f"[WebSocket] Error in position update logic: {str(e)}")

@socketio.on('store_junctions')
def handle_store_junctions(data):
    global current_junctions, current_route_data, mqtt_initialized
    try:
        origin_str = data.get('origin')
        dest_str = data.get('destination')
        if origin_str and dest_str:
            try:
                udp_payload = f"{origin_str};{dest_str}"
                udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp_sock.sendto(udp_payload.encode('utf-8'), ("127.0.0.1", 5006))
                udp_sock.close()
                print("[System] Navigation started. UDP payload sent to port 5006.")
            except Exception as udp_err:
                print(f"[UDP Error] Failed to send to port 5006: {udp_err}")

        current_junctions = data.get('junctions', [])
        current_route_data = data.get('route_data', {})
        if not mqtt_initialized:
            if init_mqtt():
                mqtt_initialized = True
        set_junctions(current_junctions)
        start_tracking()
    except Exception as e:
        pass

@socketio.on('clear_route')
def handle_clear_route():
    global current_junctions, current_route_data, mqtt_initialized, route_linestring
    try:
        stop_tracking()
        current_junctions = []
        current_route_data = {}
        route_linestring = None # Clear the memory wire
        mqtt_initialized = False 
    except Exception as e:
        pass

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
