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

# Initialize the Game Engine
game = JokerGame()

@app.route('/')
def index():
    return render_template('index.html')

# --- HELPER FUNCTION: Send Scores ---
def broadcast_scores():
    score_data = []
    for sid in game.turn_order:
        if sid not in game.players: continue
        
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

# --- CONNECT & JOIN ---
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

# --- READY & ACE HUNT ---
@socketio.on('player_ready')
def handle_ready():
    if game.mark_ready(request.sid):
        emit('log_message', {'msg': "All Ready! Hunting for Ace..."}, broadcast=True)
        sequence = game.perform_ace_hunt()
        emit('ace_hunt_animation', {'sequence': sequence}, broadcast=True)

# --- START ROUND ---
@socketio.on('start_real_round')
def handle_start_round():
    if game.game_phase == "BIDDING": return
    
    # Start round and check if we need to Declare (9 cards)
    phase_status = game.start_new_round()
    
    # Clear old scores immediately
    broadcast_scores() 

    # CASE A: SPECIAL 9-CARD ROUND (DECLARATION)
    if phase_status == "DECLARING":
        leader_sid = game.get_current_bidder_id()
        leader_name = game.players[leader_sid]['name']
        
        emit('log_message', {'msg': f"Round {game.round_number}. {leader_name} has 3 Kings... Declaring!"}, broadcast=True)
        
        # 1. Show the Leader their 3 cards so they can decide
        emit('new_round', {
            'hand': game.players[leader_sid]['hand'],
            'trump': {'rank': '?', 'suit': '?', 'value': '??'}, # Hidden for now
            'round_number': game.round_number,
            'max_bid': 9
        }, room=leader_sid)
        
        # 2. Trigger the Declaration Modal for Leader ONLY
        emit('your_turn_to_declare', {}, room=leader_sid)
        return

    # CASE B: NORMAL ROUND
    first_bidder_sid = game.get_current_bidder_id()
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

# --- NEW: HANDLE DECLARATION RESPONSE ---
@socketio.on('declare_trump')
def handle_declaration(data):
    suit = data['suit']
    # 1. Update Engine (Set Trump, Deal remaining 6 cards to leader, 9 to others)
    game.set_trump_and_deal(suit)
    
    # 2. Notify everyone of the Trump choice
    trump_display = "NO TRUMP" if suit == 'NT' else f"{suit} TRUMP"
    emit('log_message', {'msg': f"Trump declared: {trump_display}"}, broadcast=True)
    
    # 3. Refresh everyone's screen with full hands and the chosen trump card
    for pid in game.players:
        emit('new_round', {
            'hand': game.players[pid]['hand'],
            'trump': game.trump_card, # Now contains the Ace or Joker
            'round_number': game.round_number,
            'max_bid': 9
        }, room=pid)
        
    # 4. Start Bidding normally
    first_bidder_sid = game.get_current_bidder_id()
    emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(first_bidder_sid)}, room=first_bidder_sid)

# --- BIDDING ---
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
    
    broadcast_scores() # Show the new bid immediately

    if result is True: 
        emit('log_message', {'msg': "Bids closed! Game On!"}, broadcast=True)
        
        first_player_sid = game.get_current_bidder_id()
        first_name = game.players[first_player_sid]['name']
        emit('update_turn_indicator', {'sid': first_player_sid, 'name': first_name}, broadcast=True)
        emit('your_turn_to_play', {'is_leader': True}, room=first_player_sid)
    else:
        next_sid = game.get_current_bidder_id()
        emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(next_sid)}, room=next_sid)

# --- PLAYING CARDS ---
@socketio.on('play_card')
def handle_play_card(data):
    sid = request.sid
    card_index = data.get('card_index') 
    
    joker_action = data.get('joker_action')
    joker_suit = data.get('joker_suit')
    joker_data = {'joker_action': joker_action, 'joker_suit': joker_suit} if joker_action else None

    if card_index is None: return

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
        
        broadcast_scores() # Update trick count
        
        emit('log_message', {'msg': f"--- {winner['name']} wins! ---"}, broadcast=True)
        socketio.sleep(1) 
        emit('clear_table', {}, broadcast=True)
        
        if is_round_over:
            game.calculate_round_scores()
            broadcast_scores() # Update total points
            
            emit('log_message', {'msg': f"Round {game.round_number} Finished!"}, broadcast=True)
            socketio.sleep(1)
            
            # --- START NEXT ROUND ---
            phase_status = game.start_new_round()
            
            if phase_status == "GAME_OVER":
                emit('log_message', {'msg': "GAME OVER! Thanks for playing!"}, broadcast=True)
                return

            broadcast_scores() # Clear old bids

            if phase_status == "DECLARING":
                leader_sid = game.get_current_bidder_id()
                emit('new_round', {
                    'hand': game.players[leader_sid]['hand'],
                    'trump': {'rank': '?', 'suit': '?', 'value': '??'},
                    'round_number': game.round_number,
                    'max_bid': 9
                }, room=leader_sid)
                emit('your_turn_to_declare', {}, room=leader_sid)
            else:
                first_bidder_sid = game.get_current_bidder_id()
                for pid in game.players:
                    emit('new_round', {
                        'hand': game.players[pid]['hand'],
                        'trump': game.trump_card,
                        'round_number': game.round_number,
                        'max_bid': game.cards_to_deal
                    }, room=pid)
                emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(first_bidder_sid)}, room=first_bidder_sid)
                emit('your_turn_to_play', {'is_leader': True}, room=first_bidder_sid) 
        else:
            emit('update_turn_indicator', {'sid': winner['sid'], 'name': winner['name']}, broadcast=True)
            emit('your_turn_to_play', {'is_leader': True}, room=winner['sid'])
    else:
        next_sid = game.get_current_bidder_id()
        next_name = game.players[next_sid]['name']
        emit('update_turn_indicator', {'sid': next_sid, 'name': next_name}, broadcast=True)
        emit('your_turn_to_play', {'is_leader': False}, room=next_sid)

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)