import socket
import time
import sys
import math
import requests
import flatbuffers
import threading
from shapely.geometry import LineString, Polygon, Point
from shapely.ops import transform, substring
import pyproj

import gps

try:
    import CV2X.EVPacket as EVPacket
except ImportError:
    print("Error: CV2X folder not found. Run 'flatc --python cv2x_optimized.fbs' first.")
    sys.exit(1)

project_to_utm = pyproj.Transformer.from_crs('EPSG:4326', 'EPSG:32643', always_xy=True).transform

def haversine_distance(lon1, lat1, lon2, lat2):
    R = 6371000  
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi/2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2.0)**2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))

def fetch_high_res_route(start_coords, end_coords):
    url = f"http://router.project-osrm.org/route/v1/driving/{start_coords[0]},{start_coords[1]};{end_coords[0]},{end_coords[1]}?geometries=geojson&overview=full"
    response = requests.get(url).json()
    if response['code'] != 'Ok':
        raise Exception("Could not find a route between these points.")
    return response['routes'][0]['geometry']['coordinates']

def generate_forward_utm_polygon(coordinates, road_width_meters, ev_utm, lookahead_meters=300.0):
    road_radius = road_width_meters / 2.0
    center_line = LineString(coordinates)
    line_in_meters = transform(project_to_utm, center_line)
    
    ev_pt = Point(ev_utm)
    start_dist = line_in_meters.project(ev_pt)
    
    end_dist = min(start_dist + lookahead_meters, line_in_meters.length)
    forward_line = substring(line_in_meters, start_dist, end_dist)
    
    forward_coords = list(forward_line.coords)
    if ev_pt.distance(Point(forward_coords[0])) > 0.1:
        forward_coords = [ev_utm] + forward_coords
        
    snapped_forward_line = LineString(forward_coords)
    poly = snapped_forward_line.buffer(road_radius, cap_style=2, join_style=2)
    
    return poly.simplify(5.0, preserve_topology=True)


def serialize_optimized_packet(ev_gps, ev_utm, dest_gps, speed, road_width, utm_polygon=None):
    builder = flatbuffers.Builder(1024)
    offsets_x, offsets_y = [], []
    
    if utm_polygon is not None:
        coords = list(utm_polygon.exterior.coords)

        if len(coords) > 120:
            coords = coords[:120]
            
        for x, y in coords:
            val_x = max(-32768, min(32767, int(x - ev_utm[0])))
            val_y = max(-32768, min(32767, int(y - ev_utm[1])))
            offsets_x.append(val_x)
            offsets_y.append(val_y)

    EVPacket.EVPacketStartPolyOffsetXVector(builder, len(offsets_x))
    for x_val in reversed(offsets_x):
        builder.PrependInt16(x_val)
    vec_x = builder.EndVector()

    EVPacket.EVPacketStartPolyOffsetYVector(builder, len(offsets_y))
    for y_val in reversed(offsets_y):
        builder.PrependInt16(y_val)
    vec_y = builder.EndVector()

    EVPacket.EVPacketStart(builder)
    EVPacket.EVPacketAddEvLat(builder, ev_gps[1])
    EVPacket.EVPacketAddEvLon(builder, ev_gps[0])
    EVPacket.EVPacketAddEvUtmX(builder, ev_utm[0])
    EVPacket.EVPacketAddEvUtmY(builder, ev_utm[1])
    EVPacket.EVPacketAddDestLat(builder, dest_gps[1])
    EVPacket.EVPacketAddDestLon(builder, dest_gps[0])
    EVPacket.EVPacketAddSpeedKmph(builder, speed)
    EVPacket.EVPacketAddRoadWidth(builder, road_width)
    EVPacket.EVPacketAddPolyOffsetX(builder, vec_x)
    EVPacket.EVPacketAddPolyOffsetY(builder, vec_y)
    packet_obj = EVPacket.EVPacketEnd(builder)
    
    builder.Finish(packet_obj)
    return builder.Output()


shared_gps = {"coords": None}

def start_live_sender():
    BROADCAST_IP = "192.168.50.2" #"172.17.0.1" #"10.122.107.40" 
    UDP_PORT = 5005
    ROAD_WIDTH = 14.0
    MANUAL_LIVE_SPEED = 60.0 
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cmd_sock.bind(("127.0.0.1", 5006))
    cmd_sock.setblocking(False)
    print("[System] Connecting to local gpsd service...")
    try:
        gps_session = gps.gps(mode=gps.WATCH_ENABLE | gps.WATCH_NEWSTYLE)
        print("[System] ✓ gpsd connected successfully.")
    except Exception as e:
        print(f"[System] ✗ WARNING: Could not connect to gpsd: {e}")
        sys.exit(1)
        
    sampleRouteCoordinates = [
        { "lat": 8.536778560375351, "lng": 76.88374047377879 },
        { "lat": 8.536665828811161, "lng": 76.88376712184726 },
        { "lat": 8.53660904760558, "lng": 76.88377188335123 },
        { "lat": 8.53652987461416, "lng": 76.88377652450762 },
        { "lat": 8.536443816996178, "lng": 76.88377536421852 },
        { "lat": 8.536371528582087, "lng": 76.88377884508583 },
        { "lat": 8.536291777551355, "lng": 76.88376997777918 },
        { "lat": 8.536308472650193, "lng": 76.8838037420156 },
        { "lat": 8.536368831847634, "lng": 76.88384919387232 },
        { "lat": 8.536436896463083, "lng": 76.88390113885144 },
        { "lat": 8.536521656155937, "lng": 76.88393750033683 },
        { "lat": 8.53659614193106, "lng": 76.88397645907116 },
        { "lat": 8.53665521684585, "lng": 76.88401411918103 },
        { "lat": 8.53673227106876, "lng": 76.88406476553567 },
        { "lat": 8.536805472566144, "lng": 76.88410762014342 },
        { "lat": 8.536874821345572, "lng": 76.8841387871371 },
        { "lat": 8.536937748925927, "lng": 76.884173849998 },
        { "lat": 8.536983981427314, "lng": 76.88418813486726 },
        { "lat": 8.537044340517939, "lng": 76.88422969085055 },
        { "lat": 8.537086720304693, "lng": 76.88425826059286 },
        { "lat": 8.537171479853237, "lng": 76.8842374826012 },
        { "lat": 8.537269081734294, "lng": 76.88418553762207 },
        { "lat": 8.537335861954325, "lng": 76.88414917613672 },
        { "lat": 8.537420621453814, "lng": 76.8841037242789 },
        { "lat": 8.537475843537738, "lng": 76.88407515454038 },
        { "lat": 8.537536202550577, "lng": 76.88404009167948 },
        { "lat": 8.537622246233228, "lng": 76.8839972370717 },
        { "lat": 8.537712142597373, "lng": 76.88395048659049 },
        { "lat": 8.537769933110889, "lng": 76.88390503473116 },
        { "lat": 8.537834144776866, "lng": 76.88386867324577 },
        { "lat": 8.537885514101868, "lng": 76.88385438837652 },
        { "lat": 8.537947157286776, "lng": 76.88383361038267 },
        { "lat": 8.538019074318552, "lng": 76.88380504064416 },
        { "lat": 8.53810511789566, "lng": 76.88376997778096 },
        { "lat": 8.538180748395352, "lng": 76.88373708352518 },
        { "lat": 8.538414691656516, "lng": 76.88365534194833 },
        { "lat": 8.53855252405865, "lng": 76.88358844088982 },
        { "lat": 8.53869403193972, "lng": 76.88353640673321 },
        { "lat": 8.538864943985883, "lng": 76.88347322240018 }
    ]
    
    current_gps = [sampleRouteCoordinates[0]["lng"], sampleRouteCoordinates[0]["lat"]] 
    DESTINATION_COORDS = None 
    last_polygon_update_gps = None
    current_utm_polygon = None
    packets_sent = 0
    packet_size = 0 
    
    current_lookahead_meters = 300.0
    
    coord_index = 0
    last_coord_update_time = time.time()

    try:
        while True:
                last_msg = None
                try:
                    while True:
                        data, addr = cmd_sock.recvfrom(1024)
                        try:
                            last_msg = data.decode('utf-8').strip()
                        except UnicodeDecodeError:
                            print(f"[Warning] Ignored malformed binary packet from {addr}")
                            last_msg = None
                except BlockingIOError:
                    pass
                
                if last_msg:
                    print(f">>> [UDP RECEIVE] Route Update: {last_msg}")
                    try:
                        start_str, dest_str = last_msg.split(';')
                        raw_start_lat, raw_start_lng = map(float, start_str.split(','))
                        raw_dest_lat, raw_dest_lng = map(float, dest_str.split(','))
                        
                        DESTINATION_COORDS = (raw_dest_lng, raw_dest_lat)
                        
                        coord_index = 0
                        last_coord_update_time = time.time()
                        current_gps = [sampleRouteCoordinates[coord_index]["lng"], sampleRouteCoordinates[coord_index]["lat"]]
                        
                        cached_raw_route = None
                        last_polygon_update_gps = None
                        current_utm_polygon = None
                    except Exception as e:
                        print(f"Error parsing UDP destination update: {e}")
            
                # ONLY cycle and send if the destination arrived 
                if current_gps and DESTINATION_COORDS:
                    
                    current_time = time.time()
                    if current_time - last_coord_update_time >= 1.0:
                        coord_index = (coord_index + 1) % len(sampleRouteCoordinates)
                        current_gps = [sampleRouteCoordinates[coord_index]["lng"], sampleRouteCoordinates[coord_index]["lat"]]
                        last_coord_update_time = current_time

                    ev_utm = project_to_utm(current_gps[0], current_gps[1])

                    speed_mps = MANUAL_LIVE_SPEED / 3.6
                    target_lookahead = max(300.0, speed_mps * 15.0)

                    needs_update = False
                
                    if cached_raw_route is None:
                        needs_update = True
                    else:
                        dist_moved = haversine_distance(current_gps[0], current_gps[1], last_polygon_update_gps[0], last_polygon_update_gps[1])
                    
                        if abs(target_lookahead - current_lookahead_meters) > 50.0:
                            needs_update = True
                            current_lookahead_meters = target_lookahead
                        elif dist_moved >= 0.1:
                            needs_update = True

                    if needs_update:
                        if cached_raw_route is None:
                            try:
                                cached_raw_route = fetch_high_res_route(current_gps, DESTINATION_COORDS)
                                current_lookahead_meters = target_lookahead
                                current_utm_polygon = generate_forward_utm_polygon(cached_raw_route, ROAD_WIDTH, ev_utm, current_lookahead_meters)
                                last_polygon_update_gps = current_gps
                            except Exception as e:
                                print(f"API Error: {e}")
                        else:
                            try:
                                current_utm_polygon = generate_forward_utm_polygon(cached_raw_route, ROAD_WIDTH, ev_utm, current_lookahead_meters)
                                last_polygon_update_gps = current_gps
                            except Exception as e:
                                print(f"Crop Error: {e}")

                    if current_utm_polygon is not None:
                        try:
                            current_packet_binary = serialize_optimized_packet(
                                ev_gps=current_gps,
                                ev_utm=ev_utm,
                                dest_gps=DESTINATION_COORDS,
                                speed=MANUAL_LIVE_SPEED,
                                road_width=ROAD_WIDTH,
                                utm_polygon=current_utm_polygon
                            )
                
                            packet_size = len(current_packet_binary)
                            sock.sendto(current_packet_binary, (BROADCAST_IP, UDP_PORT))
                            sock.sendto(current_packet_binary, ("192.168.50.3", UDP_PORT))
                            packets_sent += 1
                        except Exception as e:
                            print(f"Serialization Error: {e}")
            
                    if packets_sent > 0 and packets_sent % 20 == 0:
                        print(f"Live GPS: {current_gps} | Lookahead: {current_lookahead_meters:.0f}m | Sent: {packets_sent} | Size: {packet_size} bytes")
                        
                time.sleep(0.01) #100hz

    except KeyboardInterrupt:
        print(f"\nShutting down. Total Packets Sent: {packets_sent}")
    finally:
        sock.close()

if __name__ == "__main__":
    start_live_sender()
