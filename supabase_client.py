# Supabase client for the drumkit worker.
# Uses the service role key to bypass RLS — same pattern as the backend
# and the audio worker.
import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is not set")
if not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not set")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)