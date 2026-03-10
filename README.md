# 🎓 Upskill Coach

An AI learning coach that sees your screen and answers your questions by voice.

Study any topic — ML, law, cooking, anything — and get real-time help from an AI that can see what you're looking at.

## How it works

1. Start the coach and tell it what you're studying
2. Open your learning material (YouTube, docs, code editor, etc.)
3. Press `v` to ask a question by voice, or `t` to type
4. The coach captures your screen, understands the context, and answers

## Demo

```
🎓 What are you studying today?
📝 : Karpathy's "Let's build GPT"

📚 Study Mode
   v: voice question
   t: text question
   q: quit

v/t/q ▶ v
🎙️  Recording... (Enter to stop)
✅ Recorded (4.2s)
📸 Capturing screen...
🔊 Coach: That's the embedding table — think of it like a dictionary
   that maps each token ID to a vector. Right now it's random,
   but it'll learn meaningful representations during training.
```

## Requirements

- macOS (uses `screencapture` for screenshots)
- Python 3.9+
- [Anthropic API key](https://console.anthropic.com/) (for AI responses)
- [OpenAI API key](https://platform.openai.com/) (for voice transcription + text-to-speech)

## Install

```bash
git clone https://github.com/jeongmokwon/upskill-coach.git
cd upskill-coach
./install.sh
```

## Set API keys

```bash
export ANTHROPIC_API_KEY="your-key-here"
export OPENAI_API_KEY="your-key-here"
```

To make keys permanent, add those lines to `~/.zshrc`.

## Run

```bash
./run.sh
```

## Commands

| Key | Action |
|-----|--------|
| `v` | Voice question (records → transcribes → captures screen → answers) |
| `t` | Text question (type → captures screen → answers) |
| `q` | Quit |

## How it's built

- **Screen capture**: macOS `screencapture`
- **AI brain**: Claude API (Anthropic) — understands screenshots + answers questions
- **Speech-to-text**: Whisper API (OpenAI)
- **Text-to-speech**: TTS API (OpenAI)
- **~200 lines of Python**. No frameworks. No dependencies beyond API clients.

## Why

I was studying ML from YouTube (Karpathy's "Let's build GPT") and kept running into the same problem: I'd see code on screen, not understand something, and have to stop, copy the code, open ChatGPT, paste it, explain what I was looking at, and ask my question. By the time I got an answer, I'd lost my flow.

This coach sits alongside whatever you're studying. It sees your screen, so you just ask "what does this line do?" and it knows what you're looking at.

## Roadmap

- [ ] Knowledge graph to track what you know/don't know
- [ ] Adaptive exercises based on your weak spots
- [ ] Spaced repetition for concepts you keep forgetting
- [ ] Proactive nudges when you're stuck or distracted
- [ ] Support for Linux/Windows

## License

MIT
