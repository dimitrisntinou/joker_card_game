import os
from flask import Flask, render_template, request, session
from flask_socketio import SocketIO, emit
from game_engine import JokerGame

# Setup Paths
base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
frontend_dir = os.path.join(base_dir, 'frontend')

app = Flask(__name__)
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
            'total_score': total_score,
            'premia': game.premia_eligible.get(sid, True)
        })

    # THIS IS THE PART THAT WAS MISSING:
    emit('update_scores', {
        'scores': score_data,
        'history': game.score_history, # Send the scoreboard data
        'turn_order': game.turn_order  # Keep columns in correct order
    }, broadcast=True)
        
    emit('update_scores', {
        'scores': score_data,
        'history': game.score_history, # Send the full table history
        'turn_order': game.turn_order  # Send the order to make columns match
    }, broadcast=True)

@socketio.on('join_game')
def handle_join(data):
    username = data['username']
    sid = request.sid
    
    # 1. RECONNECT LOGIC: Check if this username is already in the game
    existing_sid = None
    for pid, p_info in game.players.items():
        if p_info['name'] == username:
            existing_sid = pid
            break
            
    if existing_sid:
        # Swap their old broken ID for their new active ID
        game.update_player_sid(existing_sid, sid)
        
        emit('your_id', {'sid': sid}, room=sid)
        # Tell the table about the ID swap
        players_list = [{'sid': pid, 'name': game.players[pid]['name']} for pid in game.turn_order]
        emit('update_player_list', {'players': players_list}, broadcast=True)
        
        
        # Send the "care package" to instantly redraw their screen
        state_data = game.get_reconnect_state(sid)
        emit('sync_game_state', state_data, room=sid)
        
        # Refresh the scoreboard
        broadcast_scores()
        
        # If it was their turn when they closed the tab, pop the UI back up!
        if game.game_phase == "DECLARING" and game.get_current_bidder_id() == sid:
            emit('your_turn_to_declare', {}, room=sid)
        elif game.game_phase == "BIDDING" and game.get_current_bidder_id() == sid:
            emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(sid)}, room=sid)
        elif game.game_phase == "PLAYING" and game.get_current_bidder_id() == sid:
            emit('your_turn_to_play', {
                'is_leader': len(game.current_trick_cards) == 0,
                'valid_indices': game.get_valid_moves(sid)
            }, room=sid)
            
        emit('log_message', {'msg': f"🔄 {username} reconnected!"}, broadcast=True)
        return

    # 2. BRAND NEW PLAYER LOGIC (Your original code!)
    if game.add_player(sid, username):
        emit('your_id', {'sid': sid}, room=sid)
        players_list = [{'sid': pid, 'name': game.players[pid]['name']} for pid in game.turn_order]
        emit('update_player_list', {'players': players_list}, broadcast=True)
        
        if len(players_list) == 4:
            emit('enable_ready_btn', {}, broadcast=True)
    else:
        # Just a safe fallback in case a 5th person tries to join
        emit('error_message', {'msg': "Game is already full!"}, room=sid)

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
        
        emit('log_message', {'msg': f"Round {game.round_number}. {leader_name} is declaring!"}, broadcast=True)
        
        # 1. Show the Leader their 3 cards so they can decide
        emit('new_round', {
            'hand': game.players[leader_sid]['hand'],
            'trump': {'rank': '?', 'suit': '?', 'value': '??'}, # Hidden for now
            'round_number': game.round_number,
            'max_bid': 9
        }, room=leader_sid)
        
        # 2. Trigger the Declaration Modal for Leader ONLY
        emit('your_turn_to_declare', {}, room=leader_sid)
        
        # 3. --- NEW: Tell everyone else to wait! ---
        emit('wait_for_declare', {
            'leader_name': leader_name, 
            'leader_sid': leader_sid
        }, broadcast=True)
        
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
            'trump': game.trump_card, 
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

    # --- JOKER ANNOUNCEMENT BLOCK ---
    if result.get('rank') == 'Joker':
        player_name = game.players[sid]['name']
        
        # Translate the raw data into a nice readable format
        if joker_action:
            action_word = "WANTS TO TAKE" if joker_action == "TAKE" else "WANTS TO GIVE"
            suit_word = joker_suit
            
            if joker_suit == "TRUMP": suit_word = "KOZER"
            elif joker_suit == "LEAD": suit_word = "LOWEST CARD"
            elif joker_suit == "H": suit_word = "HEARTS ♥"
            elif joker_suit == "D": suit_word = "DIAMONDS ♦"
            elif joker_suit == "C": suit_word = "CLUBS ♣"
            elif joker_suit == "S": suit_word = "SPADES ♠"
            
            display_text = f"{action_word}: {suit_word}"
        else:
            display_text = "PLAYED A JOKER"
            
        emit('joker_action', {
            'name': player_name, 
            'action': display_text
        }, broadcast=True)

    result_data = game.check_trick_end()
    
    if result_data:
        winner = result_data['winner']
        is_round_over = result_data['round_over']
        
        socketio.sleep(1.5)
        
        broadcast_scores() 
        emit('log_message', {'msg': f"--- {winner['name']} wins! ---"}, broadcast=True)
        emit('animate_trick_winner', {'winner_sid': winner['sid']}, broadcast=True)
        
        socketio.sleep(0.4) 
        emit('clear_table', {}, broadcast=True)
        
        if is_round_over:
            round_log, premia_logs = game.calculate_round_scores()
            broadcast_scores() 
            
            for msg in premia_logs:
                emit('log_message', {'msg': msg}, broadcast=True)
                socketio.sleep(0.8)
                
            emit('log_message', {'msg': f"Round {game.round_number} Finished!"}, broadcast=True)
            socketio.sleep(1)

            game.ready_for_next_round = set()
            emit('show_end_round_scoreboard', {}, broadcast=True)
            
        else:
            emit('update_turn_indicator', {'sid': winner['sid'], 'name': winner['name']}, broadcast=True)
            
            # ---> NEW: SEND VALID CARDS TO THE TRICK WINNER <---
            emit('your_turn_to_play', {
                'is_leader': True, 
                'valid_indices': game.get_valid_moves(winner['sid'])
            }, room=winner['sid'])
    else:
        next_sid = game.get_current_bidder_id()
        next_name = game.players[next_sid]['name']
        emit('update_turn_indicator', {'sid': next_sid, 'name': next_name}, broadcast=True)
        
        # ---> NEW: SEND VALID CARDS TO THE NEXT PLAYER <---
        emit('your_turn_to_play', {
            'is_leader': False, 
            'valid_indices': game.get_valid_moves(next_sid)
        }, room=next_sid)


# --- WAITING FOR PLAYERS TO CLOSE SCOREBOARD ---
@socketio.on('ready_next_round')
def handle_ready_next_round():
    sid = request.sid
    game.ready_for_next_round.add(sid)
    
    player_name = game.players[sid]['name']
    emit('log_message', {'msg': f"✔️ {player_name} is ready."}, broadcast=True)
    
    # If all players have clicked ready, start the next round!
    if len(game.ready_for_next_round) == len(game.players):
        game.ready_for_next_round.clear() # Reset for next time
        
        # --- (This is where the new round logic actually belongs!) ---
        phase_status = game.start_new_round()
        
        if phase_status == "GAME_OVER":
            # 1. Sort all players by their total score (highest to lowest)
            ranked_players = sorted(game.players.values(), key=lambda p: p['score'], reverse=True)
            
            winner = ranked_players[0]
            
            # 2. Announce the results in the chat!
            emit('log_message', {'msg': "🏆 ----------------------- 🏆"}, broadcast=True)
            emit('log_message', {'msg': "GAME OVER! Final Results:"}, broadcast=True)
            emit('log_message', {'msg': f"🥇 1st Place: {winner['name']} ({winner['score']} pts)"}, broadcast=True)
            
            # Announce the runners-up
            medals = ["🥈 2nd Place", "🥉 3rd Place", "💀 4th Place"]
            for i in range(1, len(ranked_players)):
                player = ranked_players[i]
                emit('log_message', {'msg': f"{medals[i-1]}: {player['name']} ({player['score']} pts)"}, broadcast=True)
                
            emit('log_message', {'msg': "🏆 ----------------------- 🏆"}, broadcast=True)
            return

        broadcast_scores() 

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

@socketio.on('send_chat')
def handle_chat(data):
    room = session.get('room')
    nickname = session.get('nickname')
    message = data.get('message')
    
    if room and nickname and message:
        # Send the typed message to everyone at the table
        emit('receive_chat', {'nickname': nickname, 'message': message}, room=room)

if __name__ == '__main__':
    print("=========================================")
    print("🃏 JOKER SERVER IS STARTING...")
    print("🌍 Play locally at: http://localhost:7860")
    print("=========================================")
    
    # debug=True brings back your terminal logs!
    # allow_unsafe_werkzeug=True prevents a common crash when using debug mode with Socket.IO
    socketio.run(app, host='0.0.0.0', port=7860, debug=True, allow_unsafe_werkzeug=True)