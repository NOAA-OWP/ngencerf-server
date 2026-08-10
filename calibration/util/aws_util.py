import logging
import os
from functools import lru_cache
from urllib.parse import urlparse

import boto3

logger = logging.getLogger(__name__)
logging.getLogger('boto').setLevel(logging.INFO)


@lru_cache(maxsize=1)
def get_s3_client():
    return boto3.client("s3")


def parse_s3_uri(s3_uri: str) -> list[str]:
    """
    Parse an S3 URI into bucket and key components.

    :param s3_uri: A string representing the S3 URI (e.g., 's3://bucket-name/key')
    :return: A list containing the bucket name and key as strings
    """
    return s3_uri.replace("s3://", "").split("/", 1)


def download_s3(uri, save_dir):
    """
     Download a single file from S3 to a specified local directory.

     :param uri: A string representing the S3 URI of the file to download
     :param save_dir: A string representing the local directory where the file will be saved
     :return: The local file path where the file was saved
     """
    # Ensure the save directory exists; create it if it doesn't
    if not os.path.exists(save_dir):
        os.mkdir(save_dir)

    # Parse the S3 URI to extract the bucket and key
    bucket, key = parse_s3_uri(uri)
    logger.info(f'download_s3: downloading {uri} to {save_dir}')

    filename = key.split('/')[-1]
    local_file_path = os.path.join(save_dir, filename)

    # Check if the file already exists locally
    if os.path.exists(local_file_path):
        logger.info(f'download_s3: {uri} already downloaded to {local_file_path}')
    else:
        get_s3_client().download_file(bucket, key, local_file_path)
        logger.info(f'download_s3: {uri} downloaded to {local_file_path}')

    return local_file_path


def download_all_s3(uri, save_dir):
    """
    Download all files from an S3 directory to a specified local directory.

    :param uri: A string representing the S3 URI of the directory to download
    :param save_dir: A string representing the local directory where files will be saved
    """
    # Ensure the URI ends with a slash, indicating it's a directory
    if not uri.endswith('/'):
        raise Exception('uri must be a directory and end with a slash (/)')

    # Parse the S3 URI to extract the bucket and key
    bucket, key = parse_s3_uri(uri)
    logger.info(f'download_all_s3: downloading {uri} to {save_dir}')

    # Extract the subdirectory name from the key and update save_dir
    subdir = os.path.basename(os.path.dirname(key.rstrip('/')))

    save_dir = str(os.path.join(save_dir, subdir))

    # Ensure the save directory exists; create it if it doesn't
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    # List all objects in the specified S3 directory
    response = get_s3_client().list_objects_v2(Bucket=bucket, Prefix=key)
    s3_objects = [obj["Key"] for obj in response["Contents"]]

    # Download each file in the directory
    for s3_file in s3_objects:
        filename = s3_file.split('/')[-1]
        local_file_path = os.path.join(save_dir, filename)

        # Check if the file already exists locally
        if os.path.exists(local_file_path):
            logger.info(f'download_all_s3: {local_file_path} already downloaded to {local_file_path}')
        else:
            logger.info(f'download_all_s3: {uri} downloaded to {local_file_path}')
            get_s3_client().download_file(bucket, s3_file, local_file_path)
    logger.info(f"Downloaded files to {save_dir}: {', '.join(os.listdir(save_dir))}")


# def convert_s3_uri_to_fs(s3_uri: str) -> str:
#     """
#     Until Data Services gives us a file path, convert the S3 URI to a local file path.
#
#     :param s3_uri: A string representing the S3 URI (e.g., 's3://bucket-name/key')
#     :return: File path corresponding to the locally mounted bucket
#     """
#     if not s3_uri:
#         raise CerfException('S3 URI cannot be empty')
#
#     bucket, key = parse_s3_uri(s3_uri)
#
#     bucket_path = os.path.join(settings.S3_MOUNT_POINT, bucket)
#     if os.path.isdir(bucket_path):
#         return os.path.join(bucket_path, key)
#     else:
#         raise CerfException(f"Cannot find mapping for bucket '{bucket}' at '{bucket_path}'")


def list_s3_csv_files(s3_url: str) -> list[str]:
    """
    Given an S3 URL (e.g., s3://bucket-name/prefix), return a list of S3 URLs
    for .csv files under that prefix.

    :param s3_url: S3 URL representing a "directory".
    :return: List of S3 URLs to CSV files.
    """
    parsed = urlparse(s3_url)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Invalid S3 URL: {s3_url}")

    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")

    paginator = get_s3_client().get_paginator("list_objects_v2")

    csv_files = []
    found_any = False
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        contents = page.get("Contents", [])
        for obj in contents:
            found_any = True
            key = obj["Key"]
            if key.lower().endswith(".csv") and not key.endswith("/"):
                csv_files.append(f"s3://{bucket}/{key}")

    if not found_any:
        raise FileNotFoundError(f"No files found under {s3_url}")

    return sorted(csv_files)


def s3_prefix_exists(s3_url: str) -> bool:
    """
    Check if an S3 prefix (directory) exists by querying for any keys with that prefix.
    """
    parsed = urlparse(s3_url)
    if parsed.scheme != "s3" or not parsed.netloc:
        return False

    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")

    response = get_s3_client().list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return "Contents" in response and len(response["Contents"]) > 0
