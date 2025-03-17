#!/bin/bash
# filepath: /home/xx/projects/birthday-boi/set-env.sh
#
# Usage: ./set-env.sh [DEV]
# For now only "DEV" is needed for local testing.
if [ -z "$1" ]; then
    ENVIRONMENT="DEV"
else
    ENVIRONMENT=$(echo "$1" | tr '[:lower:]' '[:upper:]')
fi

export ENVIRONMENT
echo "ENVIRONMENT set to $ENVIRONMENT"