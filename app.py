import os
from flask import Flask, render_template, request, session
from flask_socketio import SocketIO, emit
from game_engine import JokerGame
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
import firebase_admin
from firebase_admin import credentials, firestore

# Setup Paths
base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
frontend_dir = os.path.join(base_dir, 'frontend')

app = Flask(__name__)
app.config['SECRET_KEY'] = 'joker_secret_key'
socketio = SocketIO(app, async_mode='eventlet')

# Initialize the Game Engine
game = JokerGame()
play_again_votes = set()

# --- FIREBASE SETUP ---
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

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

    emit('update_scores', {
        'scores': score_data,
        'history': game.score_history, # Send the scoreboard data
        'turn_order': game.turn_order  # Keep columns in correct order
    }, broadcast=True)
    
# --- AUTHENTICATION: SIGN UP ---
@socketio.on('register_user')
def handle_register(data):
    username = data.get('username').strip()
    email = data.get('email').strip().lower()
    password = data.get('password')
    birth_date = data.get('birth_date')
    sid = request.sid
    
    users_ref = db.collection('users')
    
    # 1. Check if username is already taken (Super fast because it's the Document ID!)
    if users_ref.document(username).get().exists:
        emit('error_message', {'msg': 'Username is already taken!'}, room=sid)
        return
        
    # 2. Check if email is already taken
    email_check = users_ref.where('email', '==', email).limit(1).get()
    if len(email_check) > 0:
        emit('error_message', {'msg': 'Email is already in use!'}, room=sid)
        return

    # 3. GET THE NEW UNIQUE ID (The Auto-Increment Counter)
    counter_ref = db.collection('system').document('counters')
    counter_doc = counter_ref.get()
    
    if counter_doc.exists:
        new_id = counter_doc.to_dict().get('last_user_id', 0) + 1
    else:
        new_id = 1
        
    counter_ref.set({'last_user_id': new_id})

    # 4. Hash the password and save to the Cloud!
    hashed_pw = generate_password_hash(password)
    signup_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---> THE FIX: Document ID is username, Numeric ID is saved inside! <---
    users_ref.document(username).set({
        'id': new_id,
        'username': username,
        'email': email,
        'password_hash': hashed_pw,
        'birth_date': birth_date,
        'signup_date': signup_date,
        'game_points': 1000,
        'games_played': 0
    })
    
    emit('auth_success', {'msg': f'Account created!. You can now log in.'}, room=sid)


# --- AUTHENTICATION: LOGIN ---
@socketio.on('login_user')
def handle_login(data):
    username = data.get('username').strip()
    password = data.get('password')
    sid = request.sid
    
    # Fetch the user directly using the fast Document ID method
    doc = db.collection('users').document(username).get()

    if doc.exists:
        user_data = doc.to_dict()
        # Verify the encrypted password
        if check_password_hash(user_data['password_hash'], password):
            
            # Send their lifetime stats to the browser
            stats = {
                'points': user_data.get('game_points', 1000), 
                'games_played': user_data.get('games_played', 0)
            }
            emit('login_success', {'username': username, 'stats': stats}, room=sid)
            
            # Manually route them into the lobby using your existing logic
            handle_join({'username': username})
            return

    # If document doesn't exist or password fails:
    emit('error_message', {'msg': 'Invalid Username or Password!'}, room=sid)

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

    # 2. BRAND NEW PLAYER LOGIC 
    if game.add_player(sid, username):
        emit('your_id', {'sid': sid}, room=sid)
        players_list = [{'sid': pid, 'name': game.players[pid]['name']} for pid in game.turn_order]
        emit('update_player_list', {'players': players_list}, broadcast=True)
        
        if len(players_list) == 4:
            emit('enable_ready_btn', {}, broadcast=True)
    else:
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
        
        # ---> ADDED: Tell everyone else WHO is declaring! <---
        emit('update_turn_indicator', {'sid': leader_sid, 'name': leader_name}, broadcast=True)
        
        # 3. Tell everyone else to wait
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
    
    bidder_name = game.players[first_bidder_sid]['name']
    emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(first_bidder_sid)}, room=first_bidder_sid)
    
    # ---> ADDED: Tell everyone WHO is bidding! <---
    emit('update_turn_indicator', {'sid': first_bidder_sid, 'name': bidder_name}, broadcast=True)
    
    emit('log_message', {'msg': f"Round {game.round_number}. {bidder_name} bids first."}, broadcast=True)

# --- NEW: HANDLE DECLARATION RESPONSE ---
@socketio.on('declare_trump')
def handle_declaration(data):
    suit = data['suit']
    # 1. Update Engine (Set Trump, Deal remaining cards)
    game.set_trump_and_deal(suit)
    
    # 2. Notify everyone of the Trump choice
    trump_display = "NO TRUMP" if suit == 'NT' else f"{suit} TRUMP"
    emit('log_message', {'msg': f"Trump declared: {trump_display}"}, broadcast=True)
    
    # 3. Refresh everyone's screen with full hands
    for pid in game.players:
        emit('new_round', {
            'hand': game.players[pid]['hand'],
            'trump': game.trump_card, 
            'round_number': game.round_number,
            'max_bid': 9
        }, room=pid)
        
    # 4. Start Bidding normally
    first_bidder_sid = game.get_current_bidder_id()
    first_bidder_name = game.players[first_bidder_sid]['name']
    
    emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(first_bidder_sid)}, room=first_bidder_sid)
    
    # ---> ADDED: Tell everyone WHO is bidding! <---
    emit('update_turn_indicator', {'sid': first_bidder_sid, 'name': first_bidder_name}, broadcast=True)

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
        
        # ---> THE FIX: Include valid_indices so the first player can actually click a card! <---
        emit('your_turn_to_play', {
            'is_leader': True,
            'valid_indices': game.get_valid_moves(first_player_sid)
        }, room=first_player_sid)
    else:
        next_sid = game.get_current_bidder_id()
        next_name = game.players[next_sid]['name']
        
        emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(next_sid)}, room=next_sid)
        
        # ---> ADDED: Tell everyone WHO is bidding next! <---
        emit('update_turn_indicator', {'sid': next_sid, 'name': next_name}, broadcast=True)

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
            
            # ---> THE MISSING COMMAND: Tell the trick winner to play their next card! <---
            emit('your_turn_to_play', {
                'is_leader': True, 
                'valid_indices': game.get_valid_moves(winner['sid'])
            }, room=winner['sid'])
    else:
        next_sid = game.get_current_bidder_id()
        next_name = game.players[next_sid]['name']
        emit('update_turn_indicator', {'sid': next_sid, 'name': next_name}, broadcast=True)
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
    
    # If all players have clicked ready, check what to do next!
    if len(game.ready_for_next_round) == len(game.players):
        game.ready_for_next_round.clear() # Reset for next time
        
        phase_status = game.start_new_round()
        broadcast_scores() 

        # ---> THE NEW GAME OVER & TIE BREAKER LOGIC <---
        if phase_status == "GAME_OVER":
            ranked_players = sorted(game.players.values(), key=lambda p: p['score'], reverse=True)
            highest_score = ranked_players[0]['score']
            
            # Find everyone who tied for 1st place
            winners = [p for p in ranked_players if p['score'] == highest_score]
            runners_up = [p for p in ranked_players if p['score'] < highest_score]
            
            emit('log_message', {'msg': "🏆 ----------------------- 🏆"}, broadcast=True)
            emit('log_message', {'msg': "GAME OVER! Final Results:"}, broadcast=True)
            
            winner_names = []
            for w in winners:
                emit('log_message', {'msg': f"🥇 1st Place: {w['name']} ({w['score']} pts)"}, broadcast=True)
                winner_names.append(w['name'])
                
            medals = ["🥈 2nd Place", "🥉 3rd Place", "💀 4th Place"]
            for i, p in enumerate(runners_up):
                medal = medals[i] if i < len(medals) else "💀 4th Place"
                emit('log_message', {'msg': f"{medal}: {p['name']} ({p['score']} pts)"}, broadcast=True)
                
            emit('log_message', {'msg': "🏆 ----------------------- 🏆"}, broadcast=True)
            
            # Send the LIST of winners to the frontend
            emit('game_over_event', {
                'winner_names': winner_names
            }, broadcast=True)

        elif phase_status == "DECLARING":
            leader_sid = game.get_current_bidder_id()
            leader_name = game.players[leader_sid]['name']
            
            emit('new_round', {
                'hand': game.players[leader_sid]['hand'],
                'trump': {'rank': '?', 'suit': '?', 'value': '??'},
                'round_number': game.round_number,
                'max_bid': 9
            }, room=leader_sid)
            emit('your_turn_to_declare', {}, room=leader_sid)
            emit('update_turn_indicator', {'sid': leader_sid, 'name': leader_name}, broadcast=True)
            
        else:
            first_bidder_sid = game.get_current_bidder_id()
            first_bidder_name = game.players[first_bidder_sid]['name']
            
            for pid in game.players:
                emit('new_round', {
                    'hand': game.players[pid]['hand'],
                    'trump': game.trump_card,
                    'round_number': game.round_number,
                    'max_bid': game.cards_to_deal
                }, room=pid)
                
            emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(first_bidder_sid)}, room=first_bidder_sid)
            emit('update_turn_indicator', {'sid': first_bidder_sid, 'name': first_bidder_name}, broadcast=True)

@socketio.on('play_again_vote')
def handle_play_again():
    global game  # <--- Moved to the very top!
    
    sid = request.sid
    play_again_votes.add(sid)
    
    name = game.players.get(sid, {}).get('name', 'Player')
    emit('log_message', {'msg': f"🔄 {name} voted to Play Again!"}, broadcast=True)
    
    # If all 4 players click the button...
    if len(play_again_votes) >= len(game.players):
        game = JokerGame() # Completely wipes the server's game engine clean!
        play_again_votes.clear()
        
        emit('log_message', {'msg': "Restarting game..."}, broadcast=True)
        socketio.sleep(1)
        
        # Tell all browsers to refresh and rejoin!
        emit('force_reload', {}, broadcast=True)

@socketio.on('send_chat')
def handle_chat(data):
    nickname = data.get('nickname', 'Player')
    message = data.get('message', '')
    
    if message:
        emit('receive_chat', {'nickname': nickname, 'message': message}, broadcast=True)

if __name__ == '__main__':
    print("=========================================")
    print("🃏 JOKER SERVER IS STARTING...")
    print("🌍 Play locally at: http://localhost:7860")
    print("=========================================")
    socketio.run(app, host='0.0.0.0', port=7860, debug=True, allow_unsafe_werkzeug=True)