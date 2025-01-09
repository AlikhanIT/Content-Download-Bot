from datetime import datetime

def log_action(action, details=""):
    time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{time}] {action}: {details}")
