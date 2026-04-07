#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
echo "🌐 Open http://localhost:8765 in your browser"
python coach.py
