import subprocess
import time
import sys

def run_system():
    # 1. Start the Flask Server
    print("Starting Flask API...")
    flask_proc = subprocess.Popen([sys.executable, "hostsendtestcodesample.py"])

    # 2. Start the Live Sender script
    print("Starting Live GPS Sender...")
    sender_proc = subprocess.Popen([sys.executable, "z8samplenew.py"])

    print("\n--- System is running ---")
    print("Flask is listening for destination updates.") 
    print("Sender is broadcasting GPS data.")
    print("Press Ctrl+C to stop both.")

    try:
        # Keep the main script alive while the subprocesses run
        while True:
            # Check if processes are still alive
            if flask_proc.poll() is not None:
                print("Error: Flask process died. Restarting...")
                flask_proc = subprocess.Popen([sys.executable, "hostsendtestcodesample.py"])
            
            if sender_proc.poll() is not None:
                print("Error: Sender process died. Restarting...")
                sender_proc = subprocess.Popen([sys.executable, "z8samplenew.py"])
                
            time.sleep(5)
            
    except KeyboardInterrupt:
        print("\nShutting down system...")
        flask_proc.terminate()
        sender_proc.terminate()
        print("All processes stopped.")

if __name__ == "__main__":
    run_system()                                                                                      
