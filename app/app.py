#-*- coding: utf-8 -*-

from __future__ import unicode_literals

import errno
import logging
import os
import re
import redis
import time

from flask import Flask, request, abort, send_from_directory, url_for

from linebot import (
    LineBotApi, WebhookHandler,
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    StickerMessage, StickerSendMessage,
    TemplateSendMessage, ConfirmTemplate, MessageTemplateAction,
    ButtonsTemplate, URITemplateAction, PostbackTemplateAction,
    CarouselTemplate, CarouselColumn, PostbackEvent,
    ImagemapSendMessage, MessageImagemapAction, BaseSize, ImagemapArea
)

from const import *
from utility import *
from mutex import Mutex

app = Flask(__name__)
app.config.from_object('config')
redis = redis.from_url(app.config['REDIS_URL'])
stream_handler = logging.StreamHandler()
app.logger.addHandler(stream_handler)
app.logger.setLevel(app.config['LOG_LEVEL'])
line_bot_api = LineBotApi(app.config['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(app.config['CHANNEL_SECRET'])
mapping = {"0":"0", "1":"1", "2":"2", "3":"3", "4":"5", "5":"8", "6":"13", "7":"20", "8":"40", "9":"?", "10":"∞", "11":"Soy"}

@app.route('/callback', methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info('Request body: ' + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route('/images/tmp/<number>/<filename>', methods=['GET'])
def download_result(number, filename):
    return send_from_directory(os.path.join(app.root_path, 'static', 'tmp', number), filename)

@app.route('/images/planning_poker/<size>', methods=['GET'])
def download_imagemap(size):
    filename = POKER_IMAGE_FILENAME.format(size)
    return send_from_directory(os.path.join(app.root_path, 'static', 'planning_poker'),
            filename)

@handler.add(MessageEvent, message=StickerMessage)
def handle_sticker_message(event):
    sourceId = getSourceId(event.source)
    profile = line_bot_api.get_profile(sourceId)

    if redis.get('Current'+sourceId) is not None:
        roomId = redis.get('Current'+sourceId)
    else:
        roomId = 'Room'+sourceId
        redis.set('Current'+sourceId,roomId)
    push_all_room_member(roomId, profile.display_name+'：')
    push_all_room_member_sticker(roomId,event)

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = event.message.text
    sourceId = getSourceId(event.source)
    matcher = re.match(r'^#(\d+) (.+)', text)

    roomReqStat = 'isReq' + sourceId
    profile = line_bot_api.get_profile(sourceId)

    if redis.get('Current'+sourceId) is not None:
        roomId = redis.get('Current'+sourceId)
    else:
        roomId = 'Room'+sourceId
        redis.set('Current'+sourceId,roomId)

    if text == 'LEAVE':
        redis.set(roomReqStat,'N')
        if roomId != 'Room'+sourceId:
            push_all_room_member(roomId, profile.display_name+'が退室しました')
            if redis.exists(roomId) == 1:
                redis.lrem(roomId, sourceId,0)
                redis.set('Current'+sourceId,'Room'+sourceId)
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextMessage(text='自分がオーナーの部屋からは退室できません。他メンバーを退室させます'))
            if redis.exists(roomId) == 1:
                for i in range(0,redis.llen(roomId)):
                    id_i = redis.lindex(roomId,i)
                    if id_i != sourceId:
                        redis.lrem(roomId, id_i,0)
                        line_bot_api.push_message(
                            id_i,
                            TextSendMessage(text=profile.display_name+'の部屋がクローズされました'))
    elif text == 'MEMBER':
        line_bot_api.reply_message(
            event.reply_token,
            TextMessage(text= 'この部屋に参加しているメンバー：'))
        if redis.exists(roomId) == 0:
            line_bot_api.push_message(
                sourceId, TextSendMessage(text='誰もいません'))
        else:
            for i in range(0,redis.llen(roomId)):
                profile = line_bot_api.get_profile(redis.lindex(roomId,i))
                line_bot_api.push_message(
                    sourceId, TextSendMessage(text=profile.display_name))
    elif text == 'INVITE':
        redis.set(roomReqStat,'N')
        roomId = 'Room'+sourceId
        prevRoomId = redis.get('Current'+sourceId)
        if roomId != prevRoomId:
            redis.lrem(prevRoomId, sourceId,0)
            push_all_room_member(prevRoomId, profile.display_name+'が退室しました')
        if redis.exists(roomId) ==0:
            redis.rpush(roomId,sourceId)

        redis.set('Current'+sourceId,roomId)
        line_bot_api.reply_message(
            event.reply_token,
            TextMessage(text='招待相手に部屋コードを連絡してください。部屋コードは↓です'))
        line_bot_api.push_message(
            sourceId, TextSendMessage(text=roomId))

    elif text == 'JOIN':
        redis.set(roomReqStat,'Y')
        line_bot_api.reply_message(
            event.reply_token,
            TextMessage(text='入りたい部屋コードを入力してください'))
    elif redis.get(roomReqStat) == 'Y':
        if redis.exists(text) == 1:
            no_add = False
            for i in range(0,redis.llen(text)):
                    if redis.lindex(text,i) == sourceId:
                        no_add = True
            if no_add == False:
                redis.rpush(text,sourceId)
            redis.set(roomReqStat,'N')

            redis.set('Current'+sourceId,text)
            push_all_room_member(text, profile.display_name+'が参加しました')

        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextMessage(text='部屋が見つかりません。部屋コードをもう一度入力してください'))

    elif text == 'プラポ':
        poker_mutex = Mutex(redis, POKER_MUTEX_KEY_PREFIX+ sourceId)
        poker_mutex.lock()
        if poker_mutex.is_lock():
            number = str(redis.incr(sourceId)).encode('utf-8')
            line_bot_api.reply_message(
               event.reply_token,
               generate_planning_poker_message(number))
            time.sleep(POKER_MUTEX_TIMEOUT)
            if poker_mutex.is_lock():
                poker_mutex.unlock()
    elif matcher is not None:
        number = matcher.group(1)
        value = matcher.group(2)
        current = redis.get(sourceId).encode('utf-8')
        vote_key = sourceId + number
        status = redis.hget(vote_key, 'status')
        if status is None:
            if number != current:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextMessage(text=MESSAGE_INVALID_VOTE.format(number)))
                return
            poker_mutex = Mutex(redis, POKER_MUTEX_KEY_PREFIX+ sourceId)
            vote_mutex = Mutex(redis, VOTE_MUTEX_KEY_PREFIX  + sourceId)
            location = mapping.keys()[mapping.values().index(value)]
            vote_mutex.lock()
            if vote_mutex.is_lock():
                time.sleep(VOTE_MUTEX_TIMEOUT)
                redis.hincrby(vote_key, location)
                line_bot_api.reply_message(
                    event.reply_token,
                    genenate_voting_result_message(vote_key)
                )
                redis.hset(vote_key, 'status', 'complete')
                vote_mutex.unlock()
                poker_mutex.release()
            else:
                redis.hincrby(vote_key, location)
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextMessage(text=MESSAGE_END_POKER.format(number)))
    else:
        push_all_room_member(roomId, profile.display_name+'のメッセージ：'+text)

def push_all_room_member(roomId, message):
    for i in range(0,redis.llen(roomId)):
        line_bot_api.push_message(
            redis.lindex(roomId,i),
            TextSendMessage(text=message))

def push_all_room_member_sticker(roomId, event):
    pack = event.message.package_id
    if pack == 1 or pack == 2 or pack ==3:
        for i in range(0,redis.llen(roomId)):
            line_bot_api.push_message(
                redis.lindex(roomId,i),
                StickerSendMessage(
                    package_id=event.message.package_id,
                    sticker_id=event.message.sticker_id))
    else:
        push_all_room_member(roomId,'＜スタンプ＞*対応できませんでした')


def genenate_voting_result_message(key):
    data = redis.hgetall(key)
    tmp = generate_voting_result_image(data)
    buttons_template = ButtonsTemplate(
        title='ポーカー結果',
        text='そろいましたか？',
        thumbnail_image_url='https://scrummasterbot.herokuapp.com/images/tmp/' + tmp + '/result_11.png',
        actions=[
            MessageTemplateAction(label='もう１回', text='プラポ')
    ])
    template_message = TemplateSendMessage(
        alt_text='結果', template=buttons_template)
    return template_message

def generate_planning_poker_message(number):
    message = ImagemapSendMessage(
        base_url='https://scrummasterbot.herokuapp.com/images/planning_poker',
        alt_text='planning poker',
        base_size=BaseSize(height=790, width=1040))
    actions=[]
    location=0
    for i in range(0, 3):
        for j in range(0, 4):
            actions.append(MessageImagemapAction(
                text = u'#' + number + u' ' + mapping[str(location).encode('utf-8')],
                area=ImagemapArea(
                    x=j * POKER_IMAGEMAP_ELEMENT_WIDTH,
                    y=i * POKER_IMAGEMAP_ELEMENT_HEIGHT,
                    width=(j + 1) * POKER_IMAGEMAP_ELEMENT_WIDTH,
                    height=(i + 1) * POKER_IMAGEMAP_ELEMENT_HEIGHT
                )
            ))
            location+=1
    message.actions = actions
    return message
