#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Creating virtual environment..."
/usr/bin/python3 -m venv venv

echo "Activating virtual environment..."
source venv/bin/activate

echo "Upgrading pip..."
pip install --upgrade pip

echo "Installing dependencies..."
pip install "PyMuPDF==1.24.14" Pillow

echo ""
echo "Setup complete!"
echo "To run the application:"
echo "  source venv/bin/activate && python redactor.py"
