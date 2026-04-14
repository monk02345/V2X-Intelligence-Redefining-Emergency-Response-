import sys
import os
import time
import json
import socket
import select
import threading
import http.server
import socketserver
import requests
import pyproj
import signal
import math

import matplotlib
matplotlib.use('Qt5Agg') 
import matplotlib.pyplot as plt
from shapely.geometry import Point, Polygon, LineString
from shapely.ops import transform, nearest_points, substring

from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtCore import QUrl, Qt, QTimer

# --- Allow Ctrl+C to kill the app from the terminal ---
signal.signal(signal.SIGINT, signal.SIG_DFL)

# --- Custom Imports ---
from gpstaker_phone import GPSTaker 

try:
    import CV2X.EVPacket as EVPacket
except ImportError as e:
    print(f"Error: Ev packet import failed: {e}")
    sys.exit(1)


PROXIMITY_ALERT_METERS = 75.0 
USER_BUFFER_METERS = 8.0  

# UTM Zone 43N (Meters in Kerala/Western India)
WGS84 = pyproj.CRS('EPSG:4326')
UTM_ZONE = pyproj.CRS('EPSG:32643') 
project_to_meters = pyproj.Transformer.from_crs(WGS84, UTM_ZONE, always_xy=True).transform
project_to_gps = pyproj.Transformer.from_crs(UTM_ZONE, WGS84, always_xy=True).transform

UDP_IP = "0.0.0.0"
PORT_UDP = 5005
PORT_HTTP = 8000

# === ADDED: prev_lon and prev_lat for Direction Heading ===
latest_gps = {"lon": None, "lat": None, "prev_lon": None, "prev_lat": None, "timestamp": 0.0}
cv2x_state = {
    "ev_loc_gps": None,       
    "dest_loc_gps": None,     
    "ev_loc_utm": None,       
    "speed_kmph": 0.0,        
    "route_poly_utm": None,   
    "route_poly_gps": None,
    "center_line_utm": None,  # === ADDED: For Centerline Tracing ===
    "new_poly_flag": False,     
    "packets_rx": 0             
}

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args): pass

def start_server():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT_HTTP), QuietHandler) as httpd:
        httpd.serve_forever()

threading.Thread(target=start_server, daemon=True).start()

class V2XAlertWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(1024, 768) 
        self.setWindowTitle("CRITICAL: C-V2X Proximity Alert")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)

        self.browser = QWebEngineView()
        self.browser.setUrl(QUrl(f"http://127.0.0.1:{PORT_HTTP}/map_offline.html"))
        self.setCentralWidget(self.browser)
        
        self.route_drawn = False 
        self.page_loaded = False 
        
        self.browser.loadFinished.connect(self.on_page_loaded)

    def on_page_loaded(self, ok):
        if ok:
            self.page_loaded = True

    def draw_route_once(self, lat1, lon1, lat2, lon2):
        if self.route_drawn or not self.page_loaded:
            return
            
        url = f"http://127.0.0.1:8989/route?point={lat1},{lon1}&point={lat2},{lon2}&profile=car&points_encoded=false"
        try:
            response = requests.get(url)
            coords = response.json()["paths"][0]["points"]["coordinates"]

            geojson = {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}}]
            }

            js_code = f"""
            if (typeof map !== 'undefined') {{
                if (!map.getSource('blue-route')) {{
                    map.addSource('blue-route', {{
                        'type': 'geojson',
                        'data': {json.dumps(geojson)}
                    }});
                    map.addLayer({{
                        'id': 'blue-route-line',
                        'type': 'line',
                        'source': 'blue-route',
                        'layout': {{
                            'line-join': 'round',
                            'line-cap': 'round'
                        }},
                        'paint': {{
                            'line-color': '#3b82f6',
                            'line-width': 8,
                            'line-opacity': 0.9
                        }}
                    }});
                }}

                // Add the RV Destination Marker
                let destEl = document.createElement('div');
                destEl.innerHTML = '🏁 RV Dest';
                destEl.style.background = 'black';
                destEl.style.color = 'white';
                destEl.style.padding = '4px 8px';
                destEl.style.borderRadius = '6px';
                destEl.style.fontWeight = 'bold';
                destEl.style.border = '2px solid white';
                new maplibregl.Marker(destEl).setLngLat([{lon2}, {lat2}]).addTo(map);

                // === NEW: DYNAMIC BOUNDING (AUTO-CENTER) ===
                var minLon = Math.min({lon1}, {lon2});
                var maxLon = Math.max({lon1}, {lon2});
                var minLat = Math.min({lat1}, {lat2});
                var maxLat = Math.max({lat1}, {lat2});
                
                map.fitBounds([
                    [minLon, minLat], 
                    [maxLon, maxLat]  
                ], {{
                    padding: 100,     
                    maxZoom: 17,      
                    duration: 2000    
                }});
            }}
            """
            self.browser.page().runJavaScript(js_code)
            self.route_drawn = True
        except Exception as e:
            print(f"GraphHopper Routing Error: {e}")

    def update_ev_marker(self, lon, lat):
        if not self.page_loaded: return
        js_code = f"""
        (function() {{
            if (typeof map !== 'undefined') {{
                if (typeof window.evLiveMarker === 'undefined') {{
                    let el = document.createElement('div');
                    el.innerHTML = '🚑 EV';
                    el.style.background = '#ef4444'; 
                    el.style.color = 'white';
                    el.style.padding = '5px 10px';
                    el.style.borderRadius = '8px';
                    el.style.fontWeight = 'bold';
                    el.style.border = '2px solid white';
                    el.style.boxShadow = '0 0 10px rgba(239, 68, 68, 0.8)';
                    window.evLiveMarker = new maplibregl.Marker({{element: el}}).setLngLat([{lon}, {lat}]).addTo(map);
                }} else {{
                    window.evLiveMarker.setLngLat([{lon}, {lat}]);
                }}
            }}
        }})();
        """
        self.browser.page().runJavaScript(js_code)

    def update_user_marker(self, lon, lat):
        if not self.page_loaded: return
        js_code = f"""
        (function() {{
            if (typeof map !== 'undefined') {{
                if (typeof window.rvLiveMarker === 'undefined') {{
                    let el = document.createElement('div');
                    el.innerHTML = '🚙 RV';
                    el.style.background = '#3b82f6'; 
                    el.style.color = 'white';
                    el.style.padding = '5px 10px';
                    el.style.borderRadius = '8px';
                    el.style.fontWeight = 'bold';
                    el.style.border = '2px solid white';
                    el.style.boxShadow = '0 0 10px rgba(59, 130, 246, 0.8)';
                    window.rvLiveMarker = new maplibregl.Marker({{element: el}}).setLngLat([{lon}, {lat}]).addTo(map);
                }} else {{
                    window.rvLiveMarker.setLngLat([{lon}, {lat}]);
                }}
            }}
        }})();
        """
        self.browser.page().runJavaScript(js_code)

    def update_alert_hud(self, eta_seconds, distance, speed):
        if not self.page_loaded: 
            return 
            
        mins = int(eta_seconds // 60)
        secs = int(eta_seconds % 60)
        
        js_code = f"""
        (function() {{
            let hud = document.getElementById('cv2x-alert-hud');
            if (!hud) {{
                hud = document.createElement('div');
                hud.id = 'cv2x-alert-hud';
                hud.style.cssText = 'position: absolute; top: 30px; left: 50%; transform: translateX(-50%); background: rgba(220, 38, 38, 0.95); color: white; padding: 20px 50px; border-radius: 12px; font-family: "Arial", sans-serif; text-align: center; z-index: 10000; box-shadow: 0px 10px 30px rgba(220,38,38,0.6); border: 2px solid #fca5a5;';
                
                let style = document.createElement('style');
                style.innerHTML = '@keyframes pulse-bg {{ 0% {{ background: rgba(220, 38, 38, 0.95); }} 50% {{ background: rgba(185, 28, 28, 0.95); transform: translateX(-50%) scale(1.02); }} 100% {{ background: rgba(220, 38, 38, 0.95); }} }} .emergency-pulse {{ animation: pulse-bg 1s infinite; }}';
                document.head.appendChild(style);
                hud.className = 'emergency-pulse';
                document.body.appendChild(hud);
            }}
            
            hud.innerHTML = `
                <h1 style="margin: 0; font-size: 26px; text-transform: uppercase; letter-spacing: 2px;">⚠️ Emergency Vehicle ⚠️</h1>
                <div style="font-size: 56px; font-weight: 800; margin: 5px 0;">{mins}m {secs}s</div>
                <div style="font-size: 22px; color: #fecaca;">Distance: {distance:.0f}m | Speed: {speed:.0f} km/h</div>
            `;
        }})();
        """
        self.browser.page().runJavaScript(js_code)

    def show_safe_hud(self):
        if not self.page_loaded: return
        js_code = """
        (function() {
            let hud = document.getElementById('cv2x-alert-hud');
            if (hud) {
                hud.className = ''; 
                hud.style.background = 'rgba(22, 163, 74, 0.95)'; 
                hud.style.border = '2px solid #86efac';
                hud.innerHTML = `
                    <h1 style="margin: 0; font-size: 26px; text-transform: uppercase;">✅ Area Clear</h1>
                    <div style="font-size: 24px; font-weight: bold; margin-top: 10px;">EV has passed or you are safely outside the route.</div>
                `;
            }
        })();
        """
        self.browser.page().runJavaScript(js_code)

def gps_background_thread():
    print("[GPS Thread] Live tracking started...")
    gps_taker_instance = GPSTaker()
    k=0
    while True:
        try:
            raw_lon, raw_lat = gps_taker_instance.gps_taker(k)
            k+=1
            if k>=14:
                k=0
              
            if raw_lon is not None and raw_lat is not None and raw_lon != 0.0:
                # === ADDED: Store history for Heading Calculation ===
                if latest_gps["lon"] is not None:
                    dist_approx = math.hypot(raw_lon - latest_gps["lon"], raw_lat - latest_gps["lat"]) * 111000
                    if dist_approx > 1.0: # Only update history if moved more than 1 meter
                        latest_gps["prev_lon"] = latest_gps["lon"]
                        latest_gps["prev_lat"] = latest_gps["lat"]

                latest_gps["lon"] = raw_lon
                latest_gps["lat"] = raw_lat
                latest_gps["timestamp"] = time.time()
        except Exception:
            pass
        time.sleep(1)

def udp_background_thread():
    print(f"[UDP Thread] Listening on port {PORT_UDP}...")
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            if os.name == 'nt':
                try: sock.ioctl(-1744830452, False)
                except: pass

            sock.bind((UDP_IP, PORT_UDP))
            sock.setblocking(False) 
            last_packet_time = time.time()
            
            while True:
                ready_to_read, _, _ = select.select([sock], [], [], 0.01)
                if ready_to_read:
                    latest_data = None
                    while True:
                        try:
                            data, addr = sock.recvfrom(65535)
                            latest_data = data 
                            cv2x_state["packets_rx"] += 1
                            last_packet_time = time.time() 
                        except BlockingIOError: break 
                        except Exception as e: raise e 
                            
                    if latest_data is not None:
                        try:
                            packet = EVPacket.EVPacket.GetRootAsEVPacket(latest_data, 0)
                            cv2x_state["ev_loc_gps"] = (packet.EvLon(), packet.EvLat())
                            cv2x_state["dest_loc_gps"] = (packet.DestLon(), packet.DestLat())
                            cv2x_state["speed_kmph"] = packet.SpeedKmph()
                            
                            origin_x, origin_y = packet.EvUtmX(), packet.EvUtmY()
                            cv2x_state["ev_loc_utm"] = Point(origin_x, origin_y)
                            
                            len_x = packet.PolyOffsetXLength()
                            if len_x >= 4:
                                utm_coords = [(origin_x + packet.PolyOffsetX(i), origin_y + packet.PolyOffsetY(i)) for i in range(len_x)]
                                route_poly_utm = Polygon(utm_coords)
                                cv2x_state["route_poly_utm"] = route_poly_utm
                                cv2x_state["route_poly_gps"] = transform(project_to_gps, route_poly_utm)
                                
                                # === ADDED: Extract precise centerline from polygon for accurate curve trace ===
                                boundary = list(route_poly_utm.exterior.coords)[:-1]
                                ev_pt = Point(origin_x, origin_y)
                                if boundary:
                                    start_idx = min(range(len(boundary)), key=lambda i: Point(boundary[i]).distance(ev_pt))
                                    aligned_boundary = boundary[start_idx:] + boundary[:start_idx]
                                    half = len(aligned_boundary) // 2
                                    side1 = aligned_boundary[:half]
                                    side2 = list(reversed(aligned_boundary[half:]))
                                    center_coords = []
                                    for p1, p2 in zip(side1, side2):
                                        center_coords.append(((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0))
                                    if len(center_coords) >= 2:
                                        cv2x_state["center_line_utm"] = LineString(center_coords)
                                    else:
                                        cv2x_state["center_line_utm"] = None

                                cv2x_state["new_poly_flag"] = True
                        except Exception: pass 
                
                if time.time() - last_packet_time > 2.0:
                    raise TimeoutError("Watchdog tripped! Socket frozen.")
                        
        except Exception as e:
            if 'sock' in locals():
                try: sock.close()
                except: pass

class V2XAppWrapper:
    def __init__(self):
        threading.Thread(target=gps_background_thread, daemon=True).start()
        threading.Thread(target=udp_background_thread, daemon=True).start()

        plt.ion() 
        self.fig, self.ax = plt.subplots(figsize=(10, 8))
        self.fig.canvas.manager.set_window_title("C-V2X Receiver: Ultra-Fast ETA")
        
        self.polygon_line, = self.ax.plot([], [], 'b-', linewidth=3, label="EV Route Buffer", alpha=0.3)
        self.ev_start_marker, = self.ax.plot([], [], 'g*', markersize=22, markeredgecolor='black', label="Host EV (Live)", zorder=10)
        self.dest_marker, = self.ax.plot([], [], 'kX', markersize=18, markeredgecolor='white', label="Destination", zorder=10)
        self.user_marker, = self.ax.plot([], [], marker='o', color='green', markersize=10, label="You (Receiver)", zorder=10)
        
        self.user_buffer_plot, = self.ax.plot([], [], 'r--', linewidth=1, alpha=0.5)
        self.curve_trace_line, = self.ax.plot([], [], '-', color='orange', linewidth=4, label="Proximity Trace", zorder=5)
        self.snap_line, = self.ax.plot([], [], 'r:', linewidth=2)
        
        self.info_text = self.ax.text(0.02, 0.95, 'Waiting for C-V2X Packets...', transform=self.ax.transAxes, 
                            fontsize=12, verticalalignment='top', 
                            bbox=dict(boxstyle='round', facecolor='white', alpha=0.9, edgecolor='gray'))
        
        self.ax.legend(loc="lower right")
        self.ax.grid(True, linestyle=':', alpha=0.6)

        self.alert_window = None
        self.safe_time_start = None # === ADDED: Timer for Auto-Closing the Popup ===

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_gui)
        self.timer.start(50) 

    def update_gui(self):
        if cv2x_state["new_poly_flag"]:
            px, py = cv2x_state["route_poly_gps"].exterior.xy
            self.polygon_line.set_data(px, py)
            cv2x_state["new_poly_flag"] = False

        ev_lon, ev_lat = None, None
        if cv2x_state["ev_loc_gps"] and cv2x_state["dest_loc_gps"]:
            ev_lon, ev_lat = cv2x_state["ev_loc_gps"]
            dest_lon, dest_lat = cv2x_state["dest_loc_gps"]
            self.ev_start_marker.set_data([ev_lon], [ev_lat])
            self.dest_marker.set_data([dest_lon], [dest_lat])

        if time.time() - latest_gps["timestamp"] > 5.0:
            curr_lon, curr_lat = None, None
        else:
            curr_lon = latest_gps["lon"]
            curr_lat = latest_gps["lat"]

        if ev_lat is not None and ev_lon is not None and curr_lat is not None and curr_lon is not None:
            
            eta_seconds_early = 9999 
            on_path = False
            temp_dist = 0
            temp_speed = 0
            same_direction = True # Default
            center_line = cv2x_state.get("center_line_utm")
            
            my_pt_gps = Point(curr_lon, curr_lat)
            my_pt_utm = transform(project_to_meters, my_pt_gps)
            my_buffer_utm = my_pt_utm.buffer(USER_BUFFER_METERS) 
            bx, by = transform(project_to_gps, my_buffer_utm).exterior.xy

            poly_utm = cv2x_state["route_poly_utm"]
            ev_pt_utm = cv2x_state["ev_loc_utm"]

            # === ADDED: Vector Directionality Check ===
            if latest_gps["prev_lon"] is not None and cv2x_state["dest_loc_gps"] is not None:
                rv_prev_utm = transform(project_to_meters, Point(latest_gps["prev_lon"], latest_gps["prev_lat"]))
                rv_dx = my_pt_utm.x - rv_prev_utm.x
                rv_dy = my_pt_utm.y - rv_prev_utm.y
                
                dest_gps = cv2x_state["dest_loc_gps"]
                dest_utm = transform(project_to_meters, Point(dest_gps[0], dest_gps[1]))
                ev_dx = dest_utm.x - ev_pt_utm.x
                ev_dy = dest_utm.y - ev_pt_utm.y

                if math.hypot(rv_dx, rv_dy) > 0.5: 
                    rv_angle = math.atan2(rv_dy, rv_dx)
                    ev_angle = math.atan2(ev_dy, ev_dx)
                    diff = abs(math.degrees(rv_angle - ev_angle)) % 360
                    if diff > 180: diff = 360 - diff
                    same_direction = diff < 90 # Vehicle must be facing within 90 degrees of EV target

            if poly_utm and ev_pt_utm and center_line:
                on_path = poly_utm.intersects(my_buffer_utm)
                
                # === ADDED: Math along Centerline Curve ===
                dist_ev = center_line.project(ev_pt_utm)
                dist_me = center_line.project(my_pt_utm)
                temp_dist = abs(dist_ev - dist_me) 
                
                temp_speed = max(cv2x_state["speed_kmph"] / 3.6, 1.0)
                eta_seconds_early = temp_dist / temp_speed

            # === ADDED: Alert trigger now requires same_direction ===
            alert_active = (on_path and same_direction and eta_seconds_early <= 90.0)

            # --- POPUP & AUTO-CANCEL LOGIC ---
            if alert_active:
                self.safe_time_start = None # Reset cancel timer
                if self.alert_window is None:
                    print(f"🚨 EV Approaching (ETA: {eta_seconds_early/60:.1f} mins)! Launching 3D Alert Window...")
                    self.alert_window = V2XAlertWindow()
                    self.alert_window.show()
                    self.alert_window.browser.loadFinished.connect(
                        lambda: self.alert_window.draw_route_once(ev_lat, ev_lon, curr_lat, curr_lon)
                    )
                else:
                    self.alert_window.update_ev_marker(ev_lon, ev_lat)
                    self.alert_window.update_user_marker(curr_lon, curr_lat) 
                    self.alert_window.update_alert_hud(eta_seconds_early, temp_dist, temp_speed)
            else:
                if self.alert_window is not None:
                    self.alert_window.show_safe_hud()
                    if self.safe_time_start is None:
                        self.safe_time_start = time.time()
                    elif time.time() - self.safe_time_start >= 4.0:
                        # Auto-Close the window after 4 seconds of being safe/out-of-bounds
                        self.alert_window.close()
                        self.alert_window = None
                        self.safe_time_start = None


            # --- MATPLOTLIB 2D PLOTTER LOGIC ---
            self.user_marker.set_data([curr_lon], [curr_lat])
            self.user_buffer_plot.set_data(bx, by)

            if poly_utm and ev_pt_utm and center_line:
                
                # Dynamically color the polygon based on heading match
                if same_direction:
                    self.polygon_line.set_color('blue')
                else:
                    self.polygon_line.set_color('gray') 

                if on_path:
                    self.user_marker.set_color('red')
                    self.snap_line.set_data([], [])

                    # === ADDED: Visual Trace along the exact center curve ===
                    if dist_ev < dist_me:
                        traced_utm = substring(center_line, dist_ev, dist_me)
                    else:
                        traced_utm = substring(center_line, dist_me, dist_ev)

                    if traced_utm.geom_type == 'LineString' and not traced_utm.is_empty:
                        tx, ty = transform(project_to_gps, traced_utm).xy
                        self.curve_trace_line.set_data(tx, ty)
                    else:
                        self.curve_trace_line.set_data([], [])

                    dir_status = "MATCH" if same_direction else "OPPOSITE"
                    eta_minutes = int(eta_seconds_early // 60)
                    eta_rem_seconds = int(eta_seconds_early % 60)

                    if temp_dist <= PROXIMITY_ALERT_METERS and same_direction:
                        status = f"CRITICAL: EV APPROACHING!\nDir: {dir_status} | Dist: {temp_dist:.1f}m | Speed: {cv2x_state['speed_kmph']:.0f} km/h\nETA: {eta_minutes}m {eta_rem_seconds}s\nPackets RX: {cv2x_state['packets_rx']}"
                        color = "red"
                        self.curve_trace_line.set_color('red')
                        self.fig.patch.set_facecolor('#ffe6e6')
                    else:
                        status = f"STATUS: INSIDE PATH\nDir: {dir_status} | Dist: {temp_dist:.1f}m | Speed: {cv2x_state['speed_kmph']:.0f} km/h\nETA: {eta_minutes}m {eta_rem_seconds}s\nPackets RX: {cv2x_state['packets_rx']}"
                        color = "darkorange"
                        self.curve_trace_line.set_color('orange')
                        self.fig.patch.set_facecolor('white')
                else:
                    self.user_marker.set_color('green')
                    self.curve_trace_line.set_data([], [])
                    self.fig.patch.set_facecolor('white')
                    
                    dist_to_edge = poly_utm.exterior.distance(my_buffer_utm)
                    status = f"STATUS: CLEAR\nClearance: {dist_to_edge:.1f} m\nEV Speed: {cv2x_state['speed_kmph']:.0f} km/h\nPackets RX: {cv2x_state['packets_rx']}"
                    color = "green"
                    
                    pt1_utm, pt2_utm = nearest_points(poly_utm.exterior, my_buffer_utm)
                    px = [transform(project_to_gps, pt1_utm).x, transform(project_to_gps, pt2_utm).x]
                    py = [transform(project_to_gps, pt1_utm).y, transform(project_to_gps, pt2_utm).y]
                    self.snap_line.set_data(px, py)
                    
                self.ax.relim()
                self.ax.autoscale_view()

            else:
                self.user_marker.set_color('blue')
                self.user_buffer_plot.set_color('blue')
                status = f"STATUS: WAITING FOR EV DATA...\nLive Lock: {curr_lon:.5f}, {curr_lat:.5f}\nPackets RX: {cv2x_state['packets_rx']}"
                color = "blue"
                self.fig.patch.set_facecolor('#f0f8ff')
                
                ZOOM = 0.002 
                self.ax.set_xlim(curr_lon - ZOOM, curr_lon + ZOOM)
                self.ax.set_ylim(curr_lat - ZOOM, curr_lat + ZOOM)

            self.info_text.set_text(status)
            self.info_text.set_color(color)

        else:
            # === FALLBACK IF PHONE GPS IS MISSING/STALE ===
            self.user_marker.set_data([], [])
            self.user_buffer_plot.set_data([], [])
            self.curve_trace_line.set_data([], [])
            self.snap_line.set_data([], [])
            
            if ev_lon is not None and ev_lat is not None:
                status = f"STATUS: WAITING FOR PHONE GPS...\nTracking EV...\nPackets RX: {cv2x_state['packets_rx']}"
                color = "purple"
                self.fig.patch.set_facecolor('#f3e5f5')
                
                ZOOM = 0.002
                self.ax.set_xlim(ev_lon - ZOOM, ev_lon + ZOOM)
                self.ax.set_ylim(ev_lat - ZOOM, ev_lat + ZOOM)
            else:
                status = f"STATUS: WAITING FOR ALL DATA...\nPackets RX: {cv2x_state['packets_rx']}"
                color = "blue"
                self.fig.patch.set_facecolor('white')

            self.info_text.set_text(status)
            self.info_text.set_color(color)

        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events() 

if __name__ == "__main__":
    app = QApplication(sys.argv)
    main_app = V2XAppWrapper()
    sys.exit(app.exec_())