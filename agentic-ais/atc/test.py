import asyncio
from copilot import CopilotClient

async def main():
    # Create and start client
    client = CopilotClient()
    await client.start()

    # Create a session
    session = await client.create_session({"model": "gpt-4.1"})

    # Wait for response using session.idle event
    done = asyncio.Event()

    def on_event(event):
        if event.type.value == "assistant.message":
            print(event.data.content)
        elif event.type.value == "session.idle":
            done.set()

    session.on(on_event)

    # Send a message and wait for completion
    await session.send({"prompt": "What is 2+2?"})
    await done.wait()

    # Clean up
    await session.destroy()
    await client.stop()

asyncio.run(main())
