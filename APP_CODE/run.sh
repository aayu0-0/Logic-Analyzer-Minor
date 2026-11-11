#!/bin/bash
# ESP32 Logic Analyzer - Launcher Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check if virtual environment exists
if [ -d "venv" ]; then
    echo "Activating virtual environment..."
    source venv/bin/activate
fi

# Check dependencies
python3 -c "import pyqtgraph, PyQt6" 2>/dev/null || {
    echo "Dependencies not found. Installing..."
    pip install -r requirements.txt
}

# Run the application
echo "Starting ESP32 Logic Analyzer..."
python3 main.py "$@"