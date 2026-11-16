"""Oscar as a member of WhatsApp chats: replies when someone writes @oscar.

Runs as its own process, in its own environment (.venv-whatsapp), because the
WhatsApp library needs a newer protobuf than the voice pipeline allows:

    .venv-whatsapp/bin/python -m app.whatsapp
"""
