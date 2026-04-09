#!/bin/bash
echo "🎓 Installing Upskill Coach..."
echo ""

cd "$(dirname "$0")"

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

echo ""
echo "✅ Installed!"
echo ""
echo "Next steps:"
echo "  1. Set your API key:"
echo "     export ANTHROPIC_API_KEY='your-key-here'"
echo ""
echo "  2. Run: ./run_web.sh"
