import os
import random
import string
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
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

# --- THE NEW TABLE MANAGER ARCHITECTURE ---
# active_tables holds dicts: {"code": {"game": JokerGame(), "seats": {0: None, 1: None, 2: None, 3: None}}}
active_tables = {} 
player_table_map = {} # Remembers which room code a player is in based on their SID

# --- FIREBASE SETUP ---
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

@app.route('/')
def index():
    return render_template('index.html')

# --- HELPER FUNCTION: Get Player's Current Game ---
def get_player_game(sid):
    room = player_table_map.get(sid)
    if room and room in active_tables:
        return active_tables[room]['game'], room
    return None, None

def broadcast_scores(room_code):
    game = active_tables[room_code]['game']
    score_data = []
    for sid in game.turn_order:
        if sid not in game.players: continue
        
        bid = game.bids.get(sid, 0)
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
        'history': game.score_history,
        'turn_order': game.turn_order 
    }, room=room_code)
    
# --- AUTHENTICATION: SIGN UP ---
@socketio.on('register_user')
def handle_register(data):
    username = data.get('username').strip()
    email = data.get('email').strip().lower()
    password = data.get('password')
    birth_date = data.get('birth_date')
    sid = request.sid
    
    users_ref = db.collection('users')
    
    if users_ref.document(username).get().exists:
        emit('error_message', {'msg': 'Username is already taken!'}, room=sid)
        return
        
    email_check = users_ref.where('email', '==', email).limit(1).get()
    if len(email_check) > 0:
        emit('error_message', {'msg': 'Email is already in use!'}, room=sid)
        return

    counter_ref = db.collection('system').document('counters')
    counter_doc = counter_ref.get()
    
    if counter_doc.exists:
        new_id = counter_doc.to_dict().get('last_user_id', 0) + 1
    else:
        new_id = 1
        
    counter_ref.set({'last_user_id': new_id})
    hashed_pw = generate_password_hash(password)
    signup_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
    
    doc = db.collection('users').document(username).get()

    if doc.exists:
        user_data = doc.to_dict()
        if check_password_hash(user_data['password_hash'], password):
            stats = {
                'points': user_data.get('game_points', 1000), 
                'games_played': user_data.get('games_played', 0)
            }
            # Now we just send them to the Lobby!
            emit('login_success', {'username': username, 'stats': stats}, room=sid)
            return

    emit('error_message', {'msg': 'Invalid Username or Password!'}, room=sid)

# --- AUTO-RECONNECT ON PAGE REFRESH ---
@socketio.on('auto_reconnect')
def handle_auto_reconnect(data):
    username = data.get('username')
    if not username: return
    sid = request.sid

    # 1. Search all active tables to see if this user was already playing
    for code, table in active_tables.items():
        seats = table['seats']
        game = table['game']

        old_sid = None
        for s_id, s_data in seats.items():
            if s_data and s_data['name'] == username:
                old_sid = s_data['sid']
                s_data['sid'] = sid # Update the seat map with the new connection ID!
                break

        if old_sid:
            join_room(code)
            player_table_map[sid] = code

            # CASE A: The game had already started. Warp them to the green felt!
            if old_sid in game.players:
                game.update_player_sid(old_sid, sid)
                emit('your_id', {'sid': sid}, room=sid)

                players_list = [{'sid': seats[i]['sid'], 'name': seats[i]['name']} for i in range(4) if seats[i] is not None]
                emit('update_player_list', {'players': players_list}, room=code)

                # Send the "care package" to redraw their screen instantly
                state_data = game.get_reconnect_state(sid)
                emit('sync_game_state', state_data, room=sid)
                broadcast_scores(code)

                # Check if it was their turn when they refreshed
                if game.game_phase == "DECLARING" and game.get_current_bidder_id() == sid:
                    emit('your_turn_to_declare', {}, room=sid)
                elif game.game_phase == "BIDDING" and game.get_current_bidder_id() == sid:
                    emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(sid)}, room=sid)
                elif game.game_phase == "PLAYING" and game.get_current_bidder_id() == sid:
                    emit('your_turn_to_play', {
                        'is_leader': len(game.current_trick_cards) == 0,
                        'valid_indices': game.get_valid_moves(sid)
                    }, room=sid)

                emit('log_message', {'msg': f"🔄 {username} refreshed and reconnected!"}, room=code)
                return
                
            # CASE B: They were just sitting in the chairs waiting for others.
            else:
                emit('table_joined', {'room': code, 'seats': seats}, room=sid)
                return

    # 2. If they weren't at a table at all, just silently log them into the Main Lobby
    doc = db.collection('users').document(username).get()
    if doc.exists:
        user_data = doc.to_dict()
        stats = {
            'points': user_data.get('game_points', 1000),
            'games_played': user_data.get('games_played', 0)
        }
        # This will trigger the normal login UI skip!
        emit('login_success', {'username': username, 'stats': stats}, room=sid)

# ==========================================
# --- NEW LOBBY SYSTEM ---
# ==========================================
@socketio.on('create_table')
def handle_create_table(data):
    sid = request.sid
    username = data.get('username', 'Player')
    
    # Generate 4-digit code
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    
    # Initialize the room data
    active_tables[code] = {
        'game': JokerGame(),
        'seats': {0: None, 1: None, 2: None, 3: None}
    }
    
    join_room(code)
    player_table_map[sid] = code
    
    # Send them to the seat selection screen
    emit('table_joined', {'room': code, 'seats': active_tables[code]['seats']}, room=sid)

@socketio.on('join_table')
def handle_join_table(data):
    sid = request.sid
    code = data.get('room').upper()
    username = data.get('username') # <-- We need to know who is asking!
    
    if code in active_tables:
        join_room(code)
        player_table_map[sid] = code
        
        table = active_tables[code]
        game = table['game']
        seats = table['seats']
        
        # --- SMART RECONNECT LOGIC ---
        old_sid = None
        for s_id, s_data in seats.items():
            if s_data and s_data['name'] == username:
                old_sid = s_data['sid']
                s_data['sid'] = sid # Swap the ghost ID for the new active ID
                break
                
        if old_sid:
            # 1. Put them in the lobby briefly
            emit('table_joined', {'room': code, 'seats': seats}, room=sid)
            
            # 2. If the game has already started, teleport them to the green felt!
            if old_sid in game.players:
                game.update_player_sid(old_sid, sid)
                emit('your_id', {'sid': sid}, room=sid)
                
                players_list = [{'sid': seats[i]['sid'], 'name': seats[i]['name']} for i in range(4)]
                emit('update_player_list', {'players': players_list}, room=code)
                
                # Send the "care package" to instantly redraw their cards and the table
                state_data = game.get_reconnect_state(sid)
                emit('sync_game_state', state_data, room=sid)
                broadcast_scores(code)
                
                # If it was their turn when they crashed, pop the UI back up!
                if game.game_phase == "DECLARING" and game.get_current_bidder_id() == sid:
                    emit('your_turn_to_declare', {}, room=sid)
                elif game.game_phase == "BIDDING" and game.get_current_bidder_id() == sid:
                    emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(sid)}, room=sid)
                elif game.game_phase == "PLAYING" and game.get_current_bidder_id() == sid:
                    emit('your_turn_to_play', {
                        'is_leader': len(game.current_trick_cards) == 0,
                        'valid_indices': game.get_valid_moves(sid)
                    }, room=sid)
                    
                emit('log_message', {'msg': f"🔄 {username} reconnected!"}, room=code)
            return

        # If they are just a normal player joining for the first time:
        emit('table_joined', {'room': code, 'seats': seats}, room=sid)
    else:
        emit('error_message', {'msg': "Table not found!"}, room=sid)

@socketio.on('request_table_list')
def handle_request_table_list():
    sid = request.sid
    available_tables = []
    
    # Scan all current rooms
    for code, table in active_tables.items():
        seats = table['seats']
        game = table['game']
        
        # Count how many chairs are taken
        players_seated = sum(1 for s in seats.values() if s is not None)
        
        # Only show tables that aren't full AND haven't started the game yet
        if players_seated < 4 and game.game_phase == "WAITING":
            # Figure out who the "Host" is to display their name
            host_name = "Open Table"
            for s in seats.values():
                if s is not None:
                    host_name = f"{s['name']}'s Table"
                    break
                    
            available_tables.append({
                'code': code,
                'players': players_seated,
                'host': host_name
            })
            
    emit('receive_table_list', {'tables': available_tables}, room=sid)

@socketio.on('take_seat')
def handle_take_seat(data):
    sid = request.sid
    username = data.get('username')
    seat_id = int(data.get('seat'))
    
    game, room = get_player_game(sid)
    if not game: return
    
    seats = active_tables[room]['seats']
    
    # Check if seat is taken
    if seats[seat_id] is not None:
        emit('error_message', {'msg': "Seat is already taken!"}, room=sid)
        return
        
    # Check if player is already sitting somewhere else
    for s_id, s_data in seats.items():
        if s_data and s_data['sid'] == sid:
            emit('error_message', {'msg': "You are already seated!"}, room=sid)
            return
            
    # Sit them down
    seats[seat_id] = {'sid': sid, 'name': username}
    
    # Actually add them to the JokerGame engine
    game.add_player(sid, username)
    
    # Tell everyone in the room to update the chairs
    emit('update_seats', {'seats': seats}, room=room)
    
    # Check if the table is full (all 4 seats taken)
    if all(s is not None for s in seats.values()):
        # Set the official turn order based on seats 0, 1, 2, 3
        game.turn_order = [seats[0]['sid'], seats[1]['sid'], seats[2]['sid'], seats[3]['sid']]
        
        # Tell everyone the game is starting!
        emit('log_message', {'msg': "All players seated. Game starting..."}, room=room)
        
        # Build the player list exactly how the frontend expects it
        players_list = [{'sid': seats[i]['sid'], 'name': seats[i]['name']} for i in range(4)]
        emit('update_player_list', {'players': players_list}, room=room)
        
        # Tell everyone their specific SID so the frontend can draw them at the bottom
        for i in range(4):
            emit('your_id', {'sid': seats[i]['sid']}, room=seats[i]['sid'])
            
        emit('start_game_board', {}, room=room)
        
        # ---> THE MISSING LINE: Unlock the Ready button for the whole room! <---
        emit('enable_ready_btn', {}, room=room)


# ==========================================
# --- GAME LOGIC (Updated to use Rooms) ---
# ==========================================
@socketio.on('player_ready')
def handle_ready():
    sid = request.sid
    game, room = get_player_game(sid)
    if not game: return
    
    if game.mark_ready(sid):
        emit('log_message', {'msg': "All Ready! Hunting for Ace..."}, room=room)
        sequence = game.perform_ace_hunt()
        emit('ace_hunt_animation', {'sequence': sequence}, room=room)

@socketio.on('start_real_round')
def handle_start_round():
    sid = request.sid
    game, room = get_player_game(sid)
    if not game: return
    
    if game.game_phase == "BIDDING": return
    
    phase_status = game.start_new_round()
    broadcast_scores(room) 

    if phase_status == "DECLARING":
        leader_sid = game.get_current_bidder_id()
        leader_name = game.players[leader_sid]['name']
        
        emit('log_message', {'msg': f"Round {game.round_number}. {leader_name} is declaring!"}, room=room)
        
        emit('new_round', {
            'hand': game.players[leader_sid]['hand'],
            'trump': {'rank': '?', 'suit': '?', 'value': '??'},
            'round_number': game.round_number,
            'max_bid': 9
        }, room=leader_sid)
        
        emit('your_turn_to_declare', {}, room=leader_sid)
        emit('update_turn_indicator', {'sid': leader_sid, 'name': leader_name}, room=room)
        
        emit('wait_for_declare', {
            'leader_name': leader_name, 
            'leader_sid': leader_sid
        }, room=room)
        return

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
    emit('update_turn_indicator', {'sid': first_bidder_sid, 'name': bidder_name}, room=room)
    emit('log_message', {'msg': f"Round {game.round_number}. {bidder_name} bids first."}, room=room)

@socketio.on('declare_trump')
def handle_declaration(data):
    sid = request.sid
    game, room = get_player_game(sid)
    if not game: return
    
    suit = data['suit']
    game.set_trump_and_deal(suit)
    
    trump_display = "NO TRUMP" if suit == 'NT' else f"{suit} TRUMP"
    emit('log_message', {'msg': f"Trump declared: {trump_display}"}, room=room)
    
    for pid in game.players:
        emit('new_round', {
            'hand': game.players[pid]['hand'],
            'trump': game.trump_card, 
            'round_number': game.round_number,
            'max_bid': 9
        }, room=pid)
        
    first_bidder_sid = game.get_current_bidder_id()
    first_bidder_name = game.players[first_bidder_sid]['name']
    
    emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(first_bidder_sid)}, room=first_bidder_sid)
    emit('update_turn_indicator', {'sid': first_bidder_sid, 'name': first_bidder_name}, room=room)

@socketio.on('player_bid')
def handle_bid(data):
    sid = request.sid
    game, room = get_player_game(sid)
    if not game: return
    
    amount = int(data['amount'])
    success, result = game.process_bid(sid, amount)
    if not success:
        emit('error_message', {'msg': result}, room=sid)
        return

    name = game.players[sid]['name']
    emit('log_message', {'msg': f"{name} bid {amount}"}, room=room)
    
    broadcast_scores(room)

    if result is True: 
        emit('log_message', {'msg': "Bids closed! Game On!"}, room=room)
        
        first_player_sid = game.get_current_bidder_id()
        first_name = game.players[first_player_sid]['name']
        emit('update_turn_indicator', {'sid': first_player_sid, 'name': first_name}, room=room)
        
        emit('your_turn_to_play', {
            'is_leader': True,
            'valid_indices': game.get_valid_moves(first_player_sid)
        }, room=first_player_sid)
    else:
        next_sid = game.get_current_bidder_id()
        next_name = game.players[next_sid]['name']
        
        emit('your_turn_to_bid', {'forbidden': game.get_forbidden_bid(next_sid)}, room=next_sid)
        emit('update_turn_indicator', {'sid': next_sid, 'name': next_name}, room=room)

@socketio.on('play_card')
def handle_play_card(data):
    sid = request.sid
    game, room = get_player_game(sid)
    if not game: return
    
    card_index = data.get('card_index') 
    joker_action = data.get('joker_action')
    joker_suit = data.get('joker_suit')
    joker_data = {'joker_action': joker_action, 'joker_suit': joker_suit} if joker_action else None

    if card_index is None: return

    success, result = game.play_card(sid, int(card_index), joker_data)
    
    if not success:
        emit('error_message', {'msg': result}, room=sid)
        return
        
    emit('card_played_on_table', {'sid': sid, 'card': result}, room=room)
    emit('hand_update', {'hand': game.players[sid]['hand']}, room=sid)

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
        }, room=room)

    result_data = game.check_trick_end()
    
    if result_data:
        winner = result_data['winner']
        is_round_over = result_data['round_over']

        socketio.sleep(1.5)
        
        broadcast_scores(room) 
        emit('log_message', {'msg': f"--- {winner['name']} wins! ---"}, room=room)
        emit('animate_trick_winner', {'winner_sid': winner['sid']}, room=room)
        
        socketio.sleep(0.4) 
        emit('clear_table', {}, room=room)
        
        if is_round_over:
            round_log, premia_logs = game.calculate_round_scores()
            broadcast_scores(room) 
            
            for msg in premia_logs:
                emit('log_message', {'msg': msg}, room=room)
                socketio.sleep(0.8)
                
            emit('log_message', {'msg': f"Round {game.round_number} Finished!"}, room=room)
            socketio.sleep(1)

            game.ready_for_next_round = set()
            emit('show_end_round_scoreboard', {}, room=room)
            
        else:
            emit('update_turn_indicator', {'sid': winner['sid'], 'name': winner['name']}, room=room)
            emit('your_turn_to_play', {
                'is_leader': True, 
                'valid_indices': game.get_valid_moves(winner['sid'])
            }, room=winner['sid'])
    else:
        next_sid = game.get_current_bidder_id()
        next_name = game.players[next_sid]['name']
        emit('update_turn_indicator', {'sid': next_sid, 'name': next_name}, room=room)
        emit('your_turn_to_play', {
            'is_leader': False, 
            'valid_indices': game.get_valid_moves(next_sid)
        }, room=next_sid)

@socketio.on('ready_next_round')
def handle_ready_next_round():
    sid = request.sid
    game, room = get_player_game(sid)
    if not game: return
    
    game.ready_for_next_round.add(sid)
    player_name = game.players[sid]['name']
    emit('log_message', {'msg': f"✔️ {player_name} is ready."}, room=room)
    
    if len(game.ready_for_next_round) == len(game.players):
        game.ready_for_next_round.clear() 
        
        phase_status = game.start_new_round()
        broadcast_scores(room) 

        if phase_status == "GAME_OVER":
            ranked_players = sorted(game.players.values(), key=lambda p: p['score'], reverse=True)
            highest_score = ranked_players[0]['score']
            
            winners = [p for p in ranked_players if p['score'] == highest_score]
            runners_up = [p for p in ranked_players if p['score'] < highest_score]
            
            emit('log_message', {'msg': "🏆 ----------------------- 🏆"}, room=room)
            emit('log_message', {'msg': "GAME OVER! Final Results:"}, room=room)
            
            winner_names = []
            for w in winners:
                emit('log_message', {'msg': f"🥇 1st Place: {w['name']} ({w['score']} pts)"}, room=room)
                winner_names.append(w['name'])
                
            medals = ["🥈 2nd Place", "🥉 3rd Place", "💀 4th Place"]
            for i, p in enumerate(runners_up):
                medal = medals[i] if i < len(medals) else "💀 4th Place"
                emit('log_message', {'msg': f"{medal}: {p['name']} ({p['score']} pts)"}, room=room)
                
            emit('log_message', {'msg': "🏆 ----------------------- 🏆"}, room=room)
            
            emit('game_over_event', {
                'winner_names': winner_names
            }, room=room)

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
            emit('update_turn_indicator', {'sid': leader_sid, 'name': leader_name}, room=room)
            
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
            emit('update_turn_indicator', {'sid': first_bidder_sid, 'name': first_bidder_name}, room=room)

@socketio.on('send_chat')
def handle_chat(data):
    sid = request.sid
    game, room = get_player_game(sid)
    if not game: return
    
    nickname = data.get('nickname', 'Player')
    message = data.get('message', '')
    
    if message:
        emit('receive_chat', {'nickname': nickname, 'message': message}, room=room)

if __name__ == '__main__':
    print("=========================================")
    print("🃏 JOKER SERVER IS STARTING...")
    print("🌍 Play locally at: http://localhost:7860")
    print("=========================================")
    socketio.run(app, host='0.0.0.0', port=7860, debug=True, allow_unsafe_werkzeug=True)