# Test Messaging Flow

## What I Fixed

1. **Added Supabase credential fields to TypeScript interface** in `InboxPage.tsx`
   - The interface was missing `supabase_token`, `supabase_anon_key`, `conversation_id_supabase`, `supabase_url`
   - This was preventing the receiver from getting Supabase credentials

2. **Added debug logging** to track what data is being passed around
   - In `ConversationDialog.tsx`: Logs when conversation data is set up
   - In `InboxPage.tsx`: Logs when notification is clicked and conversation is fetched

## How to Test

### Step 1: Open Two Browser Windows

1. **Window 1**: http://localhost:5173 (Test User - Initiator)
2. **Window 2**: http://localhost:5173 (Test User 1 - Receiver)

### Step 2: Login Both Users

**Window 1 (Initiator):**
- Login as "Test User" (the one you created first)

**Window 2 (Receiver):**
- Login as "Test User 1" (the second user)

### Step 3: Create Match & Send Request

**Window 1 (Initiator):**
1. Go to Matchmaking page
2. Click "Find Match"
3. Wait for match to be created
4. Click "Send Message Request"
5. You should see "Message request sent" notification

**Window 2 (Receiver):**
1. Refresh or navigate to Inbox
2. You should see a notification: "Message request from Test User"

### Step 4: Accept Conversation (CRITICAL)

**Window 2 (Receiver):**
1. Click on the notification
2. Dialog should open showing Test User's profile
3. Click "Accept" button
4. Wait for "Conversation accepted!" toast

### Step 5: Check Browser Console

**Window 2 (Receiver):**
1. Press F12 to open Developer Tools
2. Go to Console tab
3. Look for these log messages:

```
🔔 Notification clicked: {...}
📡 Fetching conversation from API...
📡 API Response: {...}
✅ Conversation fetched: {
  has_supabase_token: true,      ← SHOULD BE TRUE
  has_anon_key: true,             ← SHOULD BE TRUE  
  has_conversation_id: true,      ← SHOULD BE TRUE
  status: 'accepted'              ← SHOULD BE 'accepted'
}
📦 Setup Conversation Data: {
  status: 'accepted',
  is_initiator: false,
  has_supabase_token: true,       ← SHOULD BE TRUE
  has_anon_key: true,             ← SHOULD BE TRUE
  has_conversation_id: true,      ← SHOULD BE TRUE
  has_supabase_url: true,         ← SHOULD BE TRUE
  supabase_token_preview: 'eyJ...',
  conversation_id: '...'
}
🔍 Supabase Config: {...}        ← Should show all credentials
```

### Step 6: Test Messaging

**Window 2 (Receiver):**
1. After accepting, the conversation dialog should still be open
2. Look for:
   - ✅ "● Connected" badge (green) at the top
   - ✅ Message input field is NOT disabled (you can click it)
   - ✅ No loading spinner stuck
3. Type a message: "Hello from receiver!"
4. Click Send
5. Message should appear immediately

**Window 1 (Initiator):**
1. Go to Inbox
2. Click on the conversation notification
3. Dialog opens
4. You should see:
   - ✅ "● Connected" badge (green)
   - ✅ The message from receiver: "Hello from receiver!"
5. Type reply: "Hi from initiator!"
6. Click Send

**Window 2 (Receiver):**
- Should see the initiator's message appear in real-time (no refresh needed)

### Step 7: Network Tab Check

**Both Windows:**
1. Press F12 → Network tab
2. Filter by "supabase" or your Supabase URL
3. When sending a message, you should see:
   - POST request to `/rest/v1/messages` → **201 Created** ✅
   - No 401 or 403 errors ❌

## Expected Results

### ✅ Success Indicators

1. **Console logs show:**
   - All Supabase credentials are present (true)
   - Connection established successfully
   
2. **UI shows:**
   - Green "● Connected" badge
   - Message input is clickable (not disabled)
   - Messages send and appear instantly
   - No error toasts

3. **Network shows:**
   - POST /messages returns 201
   - GET /messages returns 200
   - Real-time subscription connects

### ❌ Failure Indicators

**If you see in console:**
```
❌ Missing Supabase configuration: {
  has_token: false,
  has_anon_key: false,
  ...
}
```
→ Backend didn't return Supabase credentials

**If you see 401/403 errors:**
→ Supabase RLS policies need to be applied (see SUPABASE_FIX.md)

**If input is disabled:**
→ Supabase connection failed, check console for errors

## Troubleshooting

### Problem: has_supabase_token is false

**Solution:** The backend isn't returning credentials. Check:
1. Conversation status is 'accepted' (not 'requested')
2. Match hasn't expired
3. Backend logs for errors in `get_conversation_by_match_service`

### Problem: 401/403 errors when sending messages

**Solution:** RLS policies not applied correctly
1. Go to Supabase Dashboard → SQL Editor
2. Run the policy fix from `SUPABASE_FIX.md`
3. Verify policies exist:
   ```sql
   SELECT policyname FROM pg_policies WHERE tablename = 'messages';
   ```

### Problem: Messages not appearing in real-time

**Solution:** Realtime subscription issue
1. Check Supabase Dashboard → Database → Replication
2. Ensure `messages` table has replication enabled
3. Check browser console for subscription errors

### Problem: "Supabase client not initialized"

**Solution:** Check the conversation data
1. Look at console logs from `📦 Setup Conversation Data`
2. Verify all 4 fields are true
3. If false, check backend response from `📡 API Response`

## Debug Commands

### Check MongoDB Conversation Status
```bash
docker exec -it <mongo-container-id> mongosh -u ConfessIt -p ConfessIt123 --authenticationDatabase admin

use ConfessDB
db.conversations.find().pretty()
```

Look for:
- `status: "accepted"` (not "requested" or "pending")
- `acceptedAt` field should exist

### Check Supabase Conversation
In Supabase Dashboard → Table Editor → `conversations`:
- Find your conversation by `match_id`
- Status should be "accepted"

### Check Backend Logs
```bash
docker logs confessit_v2-backend-1 -f
```

Look for:
- "Conversation accepted successfully"
- "Generated ephemeral token for..."
- No errors about Supabase

## Success Criteria

✅ Receiver can accept conversation
✅ Receiver sees "● Connected" badge
✅ Receiver can type and send messages
✅ Receiver sees initiator's messages in real-time
✅ Initiator sees receiver's messages in real-time
✅ No 401/403 errors in Network tab
✅ Console shows all Supabase credentials present
✅ No error toasts appear

If all criteria are met → **Messaging works! 🎉**
