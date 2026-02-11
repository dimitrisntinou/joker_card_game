import os
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
from game_engine import JokerGame

base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
frontend_dir = os.path.join(base_dir, 'frontend')

app = Flask(__name__, template_folder=frontend_dir, static_folder=frontend_dir)
app.config['SECRET_KEY'] = 'joker_secret_key'
socketio = SocketIO(app, async_mode='eventlet')

game = JokerGame()

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join(data):
    username = data['username']
    sid = request.sid
    if game.add_player(sid, username):
        emit('your_id', {'sid': sid}, room=sid)
        players_list = [{'sid': pid, 'name': game.players[pid]['name']} for pid in game.turn_order]
        emit('update_player_list', {'players': players_list}, broadcast=True)
        if len(players_list) == 4:
            emit('enable_ready_btn', {}, broadcast=True)

@socketio.on('player_ready')
def handle_ready():
    if game.mark_ready(request.sid):
        emit('log_message', {'msg': "All Ready! Hunting for Ace..."}, broadcast=True)
        sequence = game.perform_ace_hunt()
        emit('ace_hunt_animation', {'sequence': sequence}, broadcast=True)

@socketio.on('start_real_round')
def handle_start_round():
    if game.game_phase == "BIDDING": return
    first_bidder_sid = game.start_new_round()
    for pid in game.players:
        emit('new_round', {
            'hand': game.players[pid]['hand'],
            'trump': game.trump_card,
            'round_number': game.round_number,
            'max_bid': game.cards_to_deal
        }, room=pid)
    emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(first_bidder_sid)}, room=first_bidder_sid)
    bidder_name = game.players[first_bidder_sid]['name']
    emit('log_message', {'msg': f"Round {game.round_number}. {bidder_name} bids first."}, broadcast=True)

@socketio.on('player_bid')
def handle_bid(data):
    amount = int(data['amount'])
    sid = request.sid
    success, result = game.process_bid(sid, amount)
    if not success:
        emit('error_message', {'msg': result}, room=sid)
        return

    name = game.players[sid]['name']
    emit('log_message', {'msg': f"{name} bid {amount}"}, broadcast=True)
    
    if result is True: 
        emit('log_message', {'msg': "Bids closed! Game On!"}, broadcast=True)
        broadcast_scores()
        first_player_sid = game.get_current_bidder_id()
        first_name = game.players[first_player_sid]['name']
        emit('update_turn_indicator', {'sid': first_player_sid, 'name': first_name}, broadcast=True)
        # NEW: Tell the first player they are the Leader
        emit('your_turn_to_play', {'is_leader': True}, room=first_player_sid)
    else:
        next_sid = game.get_current_bidder_id()
        emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(next_sid)}, room=next_sid)

@socketio.on('play_card')
def handle_play_card(data):
    sid = request.sid
    card_index = data.get('card_index') 
    
    # NEW: Get Joker info
    joker_action = data.get('joker_action')
    joker_suit = data.get('joker_suit')
    joker_data = {'joker_action': joker_action, 'joker_suit': joker_suit} if joker_action else None

    if card_index is None: return

    # Pass joker_data to the engine
    success, result = game.play_card(sid, int(card_index), joker_data)
    
    if not success:
        emit('error_message', {'msg': result}, room=sid)
        return
        
    emit('card_played_on_table', {'sid': sid, 'card': result}, broadcast=True)
    emit('hand_update', {'hand': game.players[sid]['hand']}, room=sid)

    result_data = game.check_trick_end()
    
    if result_data:
        winner = result_data['winner']
        is_round_over = result_data['round_over']
        broadcast_scores()
        emit('log_message', {'msg': f"--- {winner['name']} wins! ---"}, broadcast=True)
        socketio.sleep(1) 
        emit('clear_table', {}, broadcast=True)
        
        if is_round_over:
            game.calculate_round_scores()
            broadcast_scores()
            emit('log_message', {'msg': f"Round {game.round_number} Finished!"}, broadcast=True)
            if game.round_number < 9: game.round_number += 1
            socketio.sleep(1)
            first_bidder_sid = game.start_new_round()
            for pid in game.players:
                emit('new_round', {'hand': game.players[pid]['hand'], 'trump': game.trump_card, 'round_number': game.round_number, 'max_bid': game.cards_to_deal}, room=pid)
            emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(first_bidder_sid)}, room=first_bidder_sid)
            # Pass is_leader=True to the first bidder
            emit('your_turn_to_play', {'is_leader': True}, room=first_bidder_sid) 
        else:
            emit('update_turn_indicator', {'sid': winner['sid'], 'name': winner['name']}, broadcast=True)
            # Pass is_leader=True to the winner (they lead the next trick)
            emit('your_turn_to_play', {'is_leader': True}, room=winner['sid'])
    else:
        next_sid = game.get_current_bidder_id()
        next_name = game.players[next_sid]['name']
        emit('update_turn_indicator', {'sid': next_sid, 'name': next_name}, broadcast=True)
        # Pass is_leader=False (they are following)
        emit('your_turn_to_play', {'is_leader': False}, room=next_sid)


def broadcast_scores():
    # Build a list of scores for all players
    score_data = []
    for sid in game.turn_order:
        bid = game.bids.get(sid, 0) # Default 0 if not bid yet
        tricks = game.tricks_won.get(sid, 0)
        has_bid = (sid in game.bids)
        total_score = game.players[sid]['score']
        
        score_data.append({
            'sid': sid,
            'bid': bid,
            'tricks': tricks,
            'has_bid': has_bid,
            'total_score': total_score
        })
        
    emit('update_scores', {'scores': score_data}, broadcast=True)

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)