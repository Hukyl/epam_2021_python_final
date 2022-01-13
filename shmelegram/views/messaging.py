from datetime import datetime
from typing import Any

from flask import request
from flask_socketio import emit, join_room, leave_room, send, close_room

from shmelegram import socketio, redis_client
from shmelegram.service import UserService, ChatService, MessageService

from shmelegram.config import ChatKind
from shmelegram.models import Chat, Message, User
from sqlalchemy.orm import load_only


JsonDict = dict[str, Any]


def datetime_to_str(dt: datetime) -> str:
    return str(dt).replace(' ', 'T')


@socketio.event
def edit_message(data: JsonDict):
    message = Message.get(data['message_id'])
    edited_at = datetime.strptime(data['edited_at'], "%Y-%m-%dT%H:%M:%S")
    message.text = data['text']
    message.edited_at = edited_at
    message.save()
    emit(
        'edit_message', data | {"chat_id": message.chat.id},
        to=message.chat.id
    )


@socketio.event
def delete_message(data: JsonDict):
    message_id = data['message_id']
    message = Message.get(message_id)
    data['chat_id'] = message.chat.id
    message.delete()
    emit('delete_message', data, to=message.chat.id)


@socketio.event
def add_view(data: JsonDict):
    message_id = data['message_id']
    user = User.get(int(redis_client.get(request.sid)))
    message = Message.get(message_id)
    message.add_view(user)
    message.save()
    emit(
        'update_view', data | {'chat_id': message.chat.id, 'user_id': user.id},
        to=message.chat.id
    )


@socketio.event
def is_offline():
    user = User.get(int(redis_client.get(request.sid)))
    user.last_online = datetime.utcnow()
    user.save()
    socketio.emit('update_user_status', {
        'user_id': user.id, 'last_online': datetime_to_str(user.last_online)
    })


@socketio.event
def is_online():
    user = User.get(int(redis_client.get(request.sid)))
    user.last_online = None
    user.save()
    socketio.emit('update_user_status', {
        'user_id': user.id, 'last_online': None
    })


@socketio.on('connect')
def connect():
    user_id: int = request.args.get("user_id")
    user = User.get(user_id)
    for chat in user.chats.options(load_only("id")).all():
        join_room(chat.id)
    redis_client.set(request.sid, user_id)
    redis_client.set(user_id, request.sid)
    is_online()


@socketio.on('disconnect')
def disconnect():
    user_id = redis_client.get(request.sid)
    is_offline()
    redis_client.delete(request.sid, user_id)


@socketio.event
def join_chat(data: JsonDict):
    chat_id = data['chat_id']
    user = User.get(
        data.get('user_id') or int(redis_client.get(request.sid))
    )
    sid = redis_client.get(user.id)
    chat = Chat.get(chat_id)
    chat.add_member(user)
    chat.save()
    message = Message(
        chat=chat, from_user=user, is_service=True,
        text=f"{user.username} joined the group"
    )
    message.save()
    emit(
        'add_member', {'user': UserService.to_json(user), 'chat_id': chat_id},
        to=chat_id, skip_sid=sid
    )
    send(MessageService.to_json(message), to=chat_id)
    join_room(chat_id, sid=sid)
    emit('add_chat', ChatService.to_json(chat), to=sid)


@socketio.event
def leave_chat(data: JsonDict):
    chat_id = data['chat_id']
    user = User.get(
        data.get('user_id') or int(redis_client.get(request.sid))
    )
    chat = Chat.get(chat_id)
    chat.remove_member(user)
    chat.save()
    leave_room(chat_id)
    emit('remove_chat', {'chat_id': chat_id}, to=request.sid)
    if chat.kind is not ChatKind.PRIVATE and chat.member_count:
        message = Message(
            chat=chat, from_user=user, is_service=True,
            text=f"{user.username} left the group"
        )
        message.save()
        emit(
            'remove_member', {'user_id': user.id, 'chat_id': chat.id},
            to=chat_id, skip_sid=request.sid
        )
        send(MessageService.to_json(message), to=chat_id)
    else:
        emit('remove_chat', {'chat_id': chat_id}, to=chat_id, skip_sid=request.sid)
        close_room(chat_id)
        chat.delete()


@socketio.event
def create_group(data: JsonDict):
    title = data['title']
    chat = Chat(kind=ChatKind.GROUP, title=title)
    user = User.get(int(redis_client.get(request.sid)))
    chat.save()
    chat.add_member(user)
    message = Message(
        chat=chat, from_user=user, is_service=True,
        text=f'{user.username} created "{title}" group'
    )
    message.save()
    chat.save()
    join_room(chat.id)
    emit('add_chat', ChatService.to_json(chat), to=chat.id)


@socketio.event
def create_private(data: JsonDict):
    chat = Chat(kind=ChatKind.PRIVATE)
    users = User.get(int(data['user_id'])), User.get(
        int(redis_client.get(request.sid))
    )
    chat.save()
    for user in users:
        chat.add_member(user)
    message = Message(
        chat=chat, from_user=users[0], is_service=True,
        text='Private chat created'
    )
    message.save()
    chat.save()
    for user in users:
        join_room(chat.id, sid=redis_client.get(user.id))
    emit('add_chat', ChatService.to_json(chat), to=chat.id)


@socketio.on('message')
def send_message(data: JsonDict):
    chat = Chat.get(data['chat_id'])
    user = User.get(int(redis_client.get(request.sid)))
    created_at = datetime.strptime(data['created_at'], '%Y-%m-%dT%H:%M:%S')
    reply_to = Message.get_or_none(data.get('reply_to'))
    message = Message(
        chat=chat, from_user=user,
        is_service=data.get('is_service', False), text=data['text'],
        reply_to=reply_to, created_at=created_at
    )
    message.save()
    message.add_view(user)
    message.save()
    send(MessageService.to_json(message), to=chat.id)
