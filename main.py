
import json
from db import db_operations
from sources import local_files
from pathlib import Path

import sqlite3

PROJECT_DIR = Path(__file__).resolve().parent

def load_config():
    config_path = PROJECT_DIR / "config.json"
    with config_path.open(encoding="utf-8") as config_file:
        return json.load(config_file)

config = load_config()
source_folder = PROJECT_DIR / config["source_data_folder"]
db_file_location = config["db_file_location"]


def versus_start():
    
    #Call all actions regarding metadata read
    data = local_files.read_all_metadata(source_folder, config)
    
    #Call all DB actions
    db_operations.load_db_data(db_file_location, data)
    
    

if __name__ == '__main__':
    versus_start()
