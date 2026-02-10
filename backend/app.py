import os
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
from game_engine import JokerGame

# Setup Paths
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
        # 1. Send ID to the new player
        emit('your_id', {'sid': sid}, room=sid)
        
        # 2. Update everyone's player list
        players_list = [{'sid': pid, 'name': game.players[pid]['name']} for pid in game.turn_order]
        emit('update_player_list', {'players': players_list}, broadcast=True)
        
        # 3. If 4 players, enable READY button
        if len(players_list) == 4:
            emit('enable_ready_btn', {}, broadcast=True)

@socketio.on('player_ready')
def handle_ready():
    if game.mark_ready(request.sid):
        # All 4 Ready -> Start Ace Hunt
        emit('log_message', {'msg': "All Ready! Hunting for Ace..."}, broadcast=True)
        sequence = game.perform_ace_hunt()
        emit('ace_hunt_animation', {'sequence': sequence}, broadcast=True)

@socketio.on('start_real_round')
def handle_start_round():
    # Only the Dealer (or winner of Ace Hunt) triggers this logic
    first_bidder_sid = game.start_new_round()
    
    # Send Hands to Everyone
    for pid in game.players:
        emit('new_round', {
            'hand': game.players[pid]['hand'],
            'trump': game.trump_card,
            'round_number': game.round_number,
            'max_bid': game.cards_to_deal
        }, room=pid)
        
    # Notify ONLY the first bidder
    emit('your_turn_to_bid', {
        'forbidden': game.get_forbidden_bid(first_bidder_sid)
    }, room=first_bidder_sid)
    
    bidder_name = game.players[first_bidder_sid]['name']
    emit('log_message', {'msg': f"Round {game.round_number}. {bidder_name} bids first."}, broadcast=True)

@socketio.on('player_bid')
def handle_bid(data):
    # 1. Get Data
    amount = int(data['amount'])
    sid = request.sid
    
    # 2. Process Bid in Engine
    # result is True ONLY if bidding is finished
    success, result = game.process_bid(sid, amount)
    
    # 3. Handle Errors (Not your turn, Forbidden bid)
    if not success:
        emit('error_message', {'msg': result}, room=sid)
        return

    # 4. Announce Bid
    name = game.players[sid]['name']
    emit('log_message', {'msg': f"{name} bid {amount}"}, broadcast=True)
    
    # 5. DECIDE NEXT STEP
    if result is True: 
        # --- CASE A: Bidding Finished -> Start Playing ---
        emit('log_message', {'msg': "Bids closed! Game On!"}, broadcast=True)
        
        # Get the first player to play (Left of Dealer)
        first_player_sid = game.get_current_bidder_id()
        
        # Tell everyone whose turn it is
        first_name = game.players[first_player_sid]['name']
        emit('update_turn_indicator', {'sid': first_player_sid, 'name': first_name}, broadcast=True)
        
        # Unlock the hand of the first player
        emit('your_turn_to_play', {}, room=first_player_sid)

    else:
        # --- CASE B: Bidding Continues -> Next Bidder ---
        next_sid = game.get_current_bidder_id()
        
        # Tell the next player it is their turn to bid
        emit('your_turn_to_bid', {
            'forbidden': game.get_forbidden_bid(next_sid)
        }, room=next_sid)

@socketio.on('play_card')
def handle_play_card(data):
    sid = request.sid
    # card_index comes from the frontend click
    # Use .get() to avoid errors if data is missing
    card_index = data.get('card_index') 
    
    if card_index is None: return

    # 1. Run Engine Logic
    success, result = game.play_card(sid, int(card_index))
    
    if not success:
        emit('error_message', {'msg': result}, room=sid)
        return
        
    played_card = result # This is the card object
    
    # 2. Show card on table for EVERYONE
    emit('card_played_on_table', {
        'sid': sid,
        'card': played_card
    }, broadcast=True)
    
    # 3. Update the player's hand (Remove the card visually)
    emit('update_hand_after_play', {'card_index': card_index}, room=sid)

    # 4. Check if Trick is Over (4 cards)
    winner_data = game.check_trick_end()
    
    if winner_data:
        # Trick Over!
        emit('log_message', {'msg': f"{winner_data['name']} takes the trick!"}, broadcast=True)
        
        # Wait 2 seconds, then clear table
        socketio.sleep(2) 
        emit('clear_table', {}, broadcast=True)
        
        # Winner leads next
        emit('update_turn_indicator', {'sid': winner_data['sid'], 'name': winner_data['name']}, broadcast=True)
        emit('your_turn_to_play', {}, room=winner_data['sid'])
        
    else:
        # Trick continues -> Next player
        next_sid = game.get_current_bidder_id()
        next_name = game.players[next_sid]['name']
        emit('update_turn_indicator', {'sid': next_sid, 'name': next_name}, broadcast=True)
        emit('your_turn_to_play', {}, room=next_sid) 

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)