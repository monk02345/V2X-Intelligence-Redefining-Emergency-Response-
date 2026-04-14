import subprocess
import re
import time

class GPSTaker:
    def __init__(self):
        # THE FIX: Ultra-forgiving Regex. 
        # It looks for "Location[", ignores the provider name, and flexibly grabs the Lat/Lon
        self.pattern = re.compile(r'Location\[.*?\s+([+-]?\d+\.\d+),\s*([+-]?\d+\.\d+)')

    def gps_taker(self,num):
        try:
            result = subprocess.run(
                ["adb", "shell", "dumpsys", "location"],
                capture_output=True,
                text=True,
                timeout=4 
            )
            
            # Find all matching location strings
            #matches = self.pattern.findall(result.stdout)
            matches=[[8.537415268737595, 76.88453274249882],[8.537321589081838, 76.88448199464894],
                     [8.537237946512635, 76.88443237452906],[8.537142036344045, 76.88438162667916],
                     [8.53706954608436, 76.88433538974925],[8.537085159372218, 76.88429366373936],
                     [8.537139805874723, 76.88425306545945],[8.537206719948765, 76.88422148901952],
                     [8.537407930175371, 76.88412277961928],[8.53744970685602, 76.88409729863625],
                     [8.53747490548658, 76.88410735691903],[8.537492809775694, 76.88413350845425],
                     [8.537514692794598, 76.88417038882444],[8.537538565177417, 76.88422872686456],
                     ]
            pair=matches[num]
            if matches:
                lat = float(pair[0])
                lon = float(pair[1])
                #print(f"[Debug] Extracted GPS: {lon}, {lat}")
                return lon, lat
            else:
        
                if "Location[" in result.stdout:
                    # Extract the raw location lines the phone generated
                    location_lines = [line.strip() for line in result.stdout.split('\n') if "Location[" in line]
                    if location_lines:
                        print(f"[DEBUG] Phone output found, but Regex missed it: {location_lines[0][:80]}...")
                return None, None
                
        except subprocess.TimeoutExpired:
            print("[Warning] ADB command timed out. Bridge is busy.")
            return None, None
        except Exception as e:
            print(f"[Error] {e}")
            return None, None

if __name__ == "__main__":
    gps_taker_instance = GPSTaker()
    print("Starting Forgiving GPS Tracker...")
    print("Ensure screen is ON and Maps is open.")
    
    # while True:
    
    #     lon, lat = gps_taker_instance.gps_taker(k)

        
    #     if lon is not None:
    #         print(f"Raw GPS Lock: {lon}, {lat}")
    #     else:
    #         print("Raw GPS: No lock / Waiting...")
            
    #     # Give the ADB bridge time to breathe (2Hz update rate)
    #     time.sleep(0.5)