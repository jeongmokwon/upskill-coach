#!/bin/bash
echo "🎓 Installing Upskill Coach..."
echo ""

cd "$(dirname "$0")"

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install anthropic openai sounddevice numpy websockets

echo ""
echo "✅ Installed!"
echo ""
echo "Next steps:"
echo "  1. Set your API keys:"
echo "     export ANTHROPIC_API_KEY='your-key-here'"
echo "     export OPENAI_API_KEY='your-key-here'"
echo ""
echo "  2. Run: ./run.sh"
