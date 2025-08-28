#!/bin/bash

# Hardcoded destination directory for the AWS RDS certificate
CERT_DIR="/ngencerf-app/aws_cert"
CERT_FILE="global-bundle.pem"
CERT_PATH="${CERT_DIR}/${CERT_FILE}"
CERT_URL="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"

echo "Downloading AWS RDS global certificate..."

# Ensure the target directory exists
mkdir -p "$CERT_DIR"

# Download the certificate
curl -sSf -o "$CERT_PATH" "$CERT_URL"

# Check if download succeeded
if [[ $? -eq 0 ]]; then
    echo "Certificate downloaded successfully to: $CERT_PATH"
else
    echo "Failed to download certificate from $CERT_URL" >&2
    exit 1
fi
