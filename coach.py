"""
Upskill Coach v3
- Knowledge Graph based learning coach
- Auto domain classification + diagnostic questions
- Screen capture + voice/text Q&A
- Mastery tracking + personalized exercises

Usage:
    source venv/bin/activate
    python coach.py
"""

import os
import sys
import base64
import subprocess
import tempfile
import time
import wave
import json
import numpy as np
import sounddevice as sd
import anthropic

sys.stdin.reconfigure(encoding='utf-8')

import kg_engine as kg
import kg_claude as kgc



# ─── Config ───────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
CHANNELS = 1
SCREENSHOT_PATH = os.path.join(tempfile.gettempdir(), "coach_screenshot.png")
AUDIO_PATH = os.path.join(tempfile.gettempdir(), "coach_audio.wav")

client = anthropic.Anthropic()

# ─── Audio/Screen ─────────────────────────────────────────────────────
recording = False
audio_frames = []
stream = None
audio_process = None


def capture_screenshot():
    subprocess.run(["screencapture", "-x", SCREENSHOT_PATH], check=True)
    with open(SCREENSHOT_PATH, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def start_recording():
    global recording, audio_frames, stream
    audio_frames = []
    recording = True
    print("🎙️  Recording... (Enter to stop)")

    def callback(indata, frames, time_info, status):
        if recording:
            audio_frames.append(indata.copy())

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16", callback=callback
    )
    stream.start()


def stop_recording():
    global recording, stream
    recording = False
    stream.stop()
    stream.close()

    if not audio_frames:
        return None

    audio_data = np.concatenate(audio_frames, axis=0)
    with wave.open(AUDIO_PATH, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_data.tobytes())

    print(f"✅ Recorded ({len(audio_data) / SAMPLE_RATE:.1f}s)")
    return AUDIO_PATH


def transcribe_audio(audio_path):
    try:
        from openai import OpenAI
        openai_client = OpenAI()
        with open(audio_path, "rb") as f:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1", file=f, language="en"
            )
        return transcript.text
    except Exception as e:
        print(f"⚠️  Whisper failed: {e}")
        return input("Type your question: ")

def speak(text):
    print(f"🔊 Coach: {text}")
    try:
        from openai import OpenAI
        openai_client = OpenAI()
        response = openai_client.audio.speech.create(
            model="tts-1", voice="nova", speed=1.07, input=text
        )
        speech_path = os.path.join(tempfile.gettempdir(), "coach_speech.mp3")
        with open(speech_path, "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
        # Play in background so user can interrupt with Enter
        global audio_process
        audio_process = subprocess.Popen(["afplay", speech_path])
    except Exception as e:
        print(f"⚠️ TTS failed: {e}")
        subprocess.run(["say", text])

# ─── Chat with Claude ────────────────────────────────────────────────
conversation_history = []

def build_system_prompt(graph, study_context=""):
    kg_summary = kg.graph_summary(graph) if graph else "No knowledge graph yet"

    return f"""You are a learning coach.

User background: {kgc.USER_BACKGROUND}

The user said they are studying: "{study_context}"
If you recognize this material from your training data, use that knowledge
when answering. Reference specific parts, structure, and progression of the
material. If the user asks about something that comes later in the material,
let them know.

{kg_summary}

Rules:
- Answer in English
- Keep it concise, 2-3 sentences (will be read aloud)
- Use Swift/iOS analogies when helpful
- If code is visible on screen, explain the key idea only
- Adjust explanation depth to user's mastery level
- Answer only. Do not add exercises."""


def ask_claude(question, screenshot_b64, graph, study_context=""):
    conversation_history.append({
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                },
            },
            {"type": "text", "text": question},
        ],
    })

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=build_system_prompt(graph, study_context),
        messages=conversation_history[-10:],
    )

    answer = response.content[0].text
    conversation_history.append({"role": "assistant", "content": answer})
    return answer

def ask_claude_simple(question, screenshot_b64, study_context):
    conversation_history.append({
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                },
            },
            {"type": "text", "text": question},
        ],
    })

    system = f"""You are a learning coach.

User background: {kgc.USER_BACKGROUND}

The user said they are studying: "{study_context}"
If you recognize this material from your training data, use that knowledge
when answering. Reference specific parts, structure, and progression of the
material. If the user asks about something that comes later in the material,
let them know.

Rules:
- Answer in English
- Keep it concise, 2-3 sentences (will be read aloud)
- Use Swift/iOS analogies when helpful
- If code is visible on screen, explain the key idea only
- Answer only. Do not add exercises.
- If you see a terminal with your own previous responses, ignore them. Focus on what the user is asking about, not the terminal output.
"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=system,
        messages=conversation_history[-10:],
    )

    answer = response.content[0].text
    conversation_history.append({"role": "assistant", "content": answer})
    return answer


def ask_claude_text(question, graph):
    conversation_history.append({"role": "user", "content": question})

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=build_system_prompt(graph),
        messages=conversation_history[-10:],
    )

    answer = response.content[0].text
    conversation_history.append({"role": "assistant", "content": answer})
    return answer


# ─── Step 1: What are you studying? ──────────────────────────────────

def ask_what_studying():
    print("🎓 What are you studying today?")
    answer = input("📝 : ").strip()
    return answer


# ─── Step 2-3: Load or create domain ─────────────────────────────────

def setup_domains(user_response):
    print("🔍 Analyzing topic...")
    domains = kgc.identify_domains(user_response)

    active_graphs = []
    for domain in domains:
        did = domain["id"]
        dname = domain["name"]
        existing = kg.load_domain(did)

        if existing:
            print(f"  📂 {dname} — loaded existing graph")
            kg.decay_mastery(existing)
            kg.save_domain(did, existing)
            active_graphs.append(existing)
        else:
            print(f"  🆕 {dname} — new domain")
            graph = kg.create_domain(did, dname)
            active_graphs.append(graph)

    return active_graphs


# ─── Step 4: Diagnostic ──────────────────────────────────────────────

def run_diagnostic(graph):
    if graph["concepts"]:
        return

    dname = graph["display_name"]
    speak(f"Let me check your {dname} level. Just 5 questions.")
    print(f"\n📋 {dname} Diagnostic")
    print("   Say 'skip' or 'idk' if you don't know.\n")

    questions = kgc.generate_diagnostic_questions(dname, n=5)

    if not questions:
        speak("Couldn't generate questions. Let's just start studying.")
        graph["level"] = "beginner"
        kg.save_domain(graph["domain_id"], graph)
        return

    results = []
    for i, q in enumerate(questions):
        print(f"  Q{i+1} [{q['difficulty']}]: {q['question']}")
        try:
            user_answer = input("  Answer: ").strip()
        except (EOFError, UnicodeDecodeError, KeyboardInterrupt) as e:
            print(f"\n  ⚠️ Input error: {e}")
            user_answer = ""

        if not user_answer or user_answer.lower() in ("skip", "idk", "pass", "don't know"):
            grade = {"correct": False, "feedback": "Skipped. No worries.", "explanation": ""}
        else:
            grade = kgc.grade_answer(q["question"], user_answer, q["concept_name"])

        correct = grade.get("correct", False)
        feedback = grade.get("feedback", "")

        if correct:
            print(f"  ✅ {feedback}")
        else:
            explanation = grade.get("explanation", "")
            if explanation:
                print(f"  ❌ {feedback} — {explanation}")
            else:
                print(f"  ❌ {feedback}")

        kg.add_concept(graph, q["concept"], q["concept_name"])
        kg.update_mastery(graph, q["concept"], correct)
        results.append({"difficulty": q["difficulty"], "correct": correct})

    graph["level"] = kgc.determine_level(results)
    kg.save_domain(graph["domain_id"], graph)

    speak(f"Done! Your {dname} level is {graph['level']}. Let's go!")
    print(f"\n  📊 Level: {graph['level']}")
    kg.graph_status_display(graph)


# ─── Step 5-6: Study loop ────────────────────────────────────────────

def get_voice_question():
    start_recording()
    try:
        input("  ⏹ Enter to stop recording...")
    except (EOFError, UnicodeDecodeError, KeyboardInterrupt):
        pass
    audio_path = stop_recording()

    if audio_path:
        print("📝 Transcribing...")
        question = transcribe_audio(audio_path)
        print(f"🗣️ : {question}")
        return question
    return None


def study_loop(study_context):
    print(f"\n📚 Study Mode")
    print("   v: voice question")
    print("   t: text question")
    print("   q: quit\n")

    while True:
        try:
            cmd = input("v/t/q ▶ ").strip().lower()
        except (EOFError, UnicodeDecodeError, KeyboardInterrupt):
            break

        if audio_process and audio_process.poll() is None:
            audio_process.terminate()

        if cmd == "q":
            break

        elif cmd == "t":
            try:
                question = input("Ask a question: ")
            except (EOFError, UnicodeDecodeError, KeyboardInterrupt):
                continue
            if not question.strip() or len(question.strip()) < 3:
                continue
            print("📸 Capturing screen...")
            screenshot_b64 = capture_screenshot()
            answer = ask_claude_simple(question, screenshot_b64, study_context)
            speak(answer)

        elif cmd == "v":
            question = get_voice_question()
            noise_words = {"you", "the", "a", "an", "um", "uh", "hmm", "oh", "ah"}
            if not question or question.strip().lower() in noise_words:
                print("  ⏭ Nothing was asked.")
                continue
            print("📸 Capturing screen...")
            screenshot_b64 = capture_screenshot()
            answer = ask_claude_simple(question, screenshot_b64, study_context)
            speak(answer)

        else:
            if cmd:
                print("   v=voice, t=text, q=quit")

    speak("Good work. See you next time.")

def auto_extract_concepts(graph, conversation_text):
    try:
        existing = list(graph["concepts"].keys())
        new_concepts = kgc.extract_concepts_from_conversation(
            graph["display_name"], conversation_text, existing
        )
        for c in new_concepts:
            kg.add_concept(graph, c["id"], c["name"], c.get("prerequisites", []))
        if new_concepts:
            names = [c["name"] for c in new_concepts]
            print(f"  📎 New concepts: {', '.join(names)}")
            kg.save_domain(graph["domain_id"], graph)
    except Exception:
        pass


# ─── Review mode ──────────────────────────────────────────────────────

def run_review(graph):
    weak = kg.get_weakest_concepts(graph, n=3)
    ready = kg.get_ready_concepts(graph)
    targets = ready[:3] if ready else weak[:3]

    if not targets:
        speak("Nothing to review! Time to learn something new.")
        return

    names = [c[1]["display_name"] for c in targets]
    speak(f"Let's practice. Starting with {names[0]}.")

    for concept_id, concept in targets:
        cname = concept["display_name"]
        mastery = concept["mastery"]
        print(f"\n📝 [{cname}] (mastery: {mastery:.0%})")

        exercise = kgc.generate_exercise(
            graph["display_name"], cname, mastery
        )
        print(f"   {exercise}")
        speak("Check the question on screen.")

        try:
            user_answer = input("   Answer (? for hint): ").strip()
        except (EOFError, UnicodeDecodeError, KeyboardInterrupt):
            continue

        if not user_answer:
            print("   ⏭ Skipped")
            continue

        if user_answer.startswith("?"):
            hint = ask_claude_text(
                f"Give me a hint for: {exercise}\nMy question: {user_answer[1:].strip()}",
                graph
            )
            speak(hint)
            try:
                user_answer = input("   Answer: ").strip()
            except (EOFError, UnicodeDecodeError, KeyboardInterrupt):
                continue
            if not user_answer:
                continue

        grade = kgc.grade_answer(exercise, user_answer, cname)
        correct = grade.get("correct", False)
        feedback = grade.get("feedback", "")
        explanation = grade.get("explanation", "")

        if correct:
            print(f"   ✅ {feedback}")
            speak(f"Correct! {feedback}")
        else:
            msg = f"{feedback} {explanation}".strip()
            print(f"   ❌ {msg}")
            speak(msg)

        kg.update_mastery(graph, concept_id, correct)
        kg.save_domain(graph["domain_id"], graph)
        print(f"   📊 Mastery: {concept['mastery']:.0%}")

    print("\n✅ Review done!")


# ─── Main ─────────────────────────────────────────────────────────────

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ Please set ANTHROPIC_API_KEY")
        sys.exit(1)

    print("=" * 50)
    print("🎓 Upskill Coach")
    print("=" * 50)

    print("🎓 What are you studying today?")
    study_context = input("📝 : ").strip()

    study_loop(study_context)


if __name__ == "__main__":
    main()
