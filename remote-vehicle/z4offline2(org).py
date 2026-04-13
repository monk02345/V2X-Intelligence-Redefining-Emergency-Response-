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

import matplotlib
matplotlib.use('Qt5Agg') 
import matplotlib.pyplot as plt
from shapely.geometry import Point, Polygon
from shapely.ops import transform, nearest_points

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

latest_gps = {"lon": None, "lat": None, "timestamp": 0.0}
cv2x_state = {
    "ev_loc_gps": None,       
    "dest_loc_gps": None,     
    "ev_loc_utm": None,       
    "speed_kmph": 0.0,        
    "route_poly_utm": None,   
    "route_poly_gps": None,
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
                // Calculate the corners of the box containing both vehicles
                var minLon = Math.min({lon1}, {lon2});
                var maxLon = Math.max({lon1}, {lon2});
                var minLat = Math.min({lat1}, {lat2});
                var maxLat = Math.max({lat1}, {lat2});
                
                // Tell the map to smoothly fly to that box
                map.fitBounds([
                    [minLon, minLat], // Southwestern corner
                    [maxLon, maxLat]  // Northeastern corner
                ], {{
                    padding: 100,     // Leaves a 100-pixel visual buffer around the edges
                    maxZoom: 17,      // Prevents zooming too close if vehicles are right next to each other
                    duration: 2000    // Smooth 2-second cinematic pan
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
                latest_gps["lon"] = raw_lon
                latest_gps["lat"] = raw_lat
                latest_gps["timestamp"] = time.time()
        except Exception:
            pass
        time.sleep(0.5)

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
        self.ev_start_marker, = self.ax.plot([], [], 'g*', markersize
