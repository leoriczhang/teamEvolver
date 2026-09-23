#!/bin/sh

set -eu

echo "Installing packages..."
python -m pip install --no-index --no-build-isolation --find-links=/app/deploy/site-packages -r /app/deploy/requirements.txt

# Install teamEvolver itself (provides the `teamEvolver` console command)
python -m pip install --no-index --find-links=/app/deploy/site-packages teamEvolver

echo "Install finished."
