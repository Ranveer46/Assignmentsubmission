import os
from backend.env_loader import load_project_dotenv
load_project_dotenv()
print(f"GOOGLE_API_KEY: {os.getenv('GOOGLE_API_KEY')}")
