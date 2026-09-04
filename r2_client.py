# R2 client for the drumkit worker.
# Handles downloading staged files, uploading deduped samples/waveforms,
# and cleaning up staging objects after a job completes.
import os
import boto3
from dotenv import load_dotenv

load_dotenv()

R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = os.environ["R2_BUCKET_NAME"]

s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)


def download_from_r2(key: str, local_path: str) -> None:
    """Download a file from R2 to a local temp path."""
    s3.download_file(R2_BUCKET_NAME, key, local_path)


def upload_to_r2(local_path: str, key: str, content_type: str) -> None:
    """Upload a local file to R2 at the given key."""
    s3.upload_file(
        local_path,
        R2_BUCKET_NAME,
        key,
        ExtraArgs={"ContentType": content_type},
    )


def delete_from_r2(key: str) -> None:
    """Delete an object from R2. Used to clean up staging files after a job finishes."""
    s3.delete_object(Bucket=R2_BUCKET_NAME, Key=key)