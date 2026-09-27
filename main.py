
import json
from db import db_operations
from sources import local_files
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent

def load_config():
    config_path = PROJECT_DIR / "config.json"
    with config_path.open(encoding="utf-8") as config_file:
        return json.load(config_file)

config = load_config()
configured_source = Path(config["source_data_folder"]).expanduser()
source_folder = (configured_source if configured_source.is_absolute() else PROJECT_DIR / configured_source).resolve()
configured_db = Path(config["db_file_location"]).expanduser()
db_file_location = (configured_db if configured_db.is_absolute() else PROJECT_DIR / configured_db).resolve()


def versus_start():
    
    #Call all actions regarding metadata read
    data = local_files.read_all_metadata(source_folder, config)
    
    #Call all DB actions
    db_operations.load_db_data(db_file_location, data)
    

if __name__ == '__main__':
    versus_start()
