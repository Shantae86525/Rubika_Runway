"""
Unified entrypoint.

  MODE=master  (default) -> run the Telegram panel  (bot.py)
  MODE=worker            -> run the headless worker API node (worker_api.py)

The mode is read from the environment / .env via config.MODE. This lets the
exact same codebase + Docker image serve as either a master or a worker, which
is what the auto-provisioning flow relies on.

Run locally:
    python main.py                 # master (or set MODE=worker)
"""
import config


def main():
    if config.MODE == "worker":
        import worker_api
        worker_api.run()
    else:
        import asyncio
        import bot
        asyncio.run(bot.amain())


if __name__ == "__main__":
    main()
