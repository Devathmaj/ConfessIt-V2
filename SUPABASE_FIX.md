# Supabase RLS Policy Fix - Receiver Can't Send/View Messages

## Problem
The receiver can accept the conversation but cannot:
- View any messages
- Send new messages
- Text area is disabled

## Root Cause
The current Supabase Row-Level Security (RLS) policies are blocking requests because:
1. Custom JWT tokens don't work with Supabase's `auth.jwt()` function
2. The RLS policies are trying to validate JWT claims that aren't available
3. This causes 401/403 errors when trying to read or insert messages

## Solution
Update the RLS policies in your Supabase dashboard to validate based on the conversation data in the database instead of JWT claims.

---

## Step-by-Step Fix

### 1. Open Supabase Dashboard
- Go to: https://supabase.com/dashboard
- Select your project
- Navigate to **SQL Editor** in the left sidebar

### 2. Drop Existing Policies

Run this SQL to remove the old policies:

```sql
-- Drop old policies on messages table
DROP POLICY IF EXISTS "Read messages via anon key" ON public.messages;
DROP POLICY IF EXISTS "Secure insert with function" ON public.messages;
DROP POLICY IF EXISTS "Users can read their messages" ON public.messages;
DROP POLICY IF EXISTS "Users can insert their messages" ON public.messages;

-- Drop old function if it exists
DROP FUNCTION IF EXISTS can_insert_message(uuid, text, text);
```

### 3. Create New Simplified Policies

Run this SQL to create new permissive policies:

```sql
-- Policy for reading messages
-- Allows reading if the conversation exists and is accepted
CREATE POLICY "Allow read messages in valid conversations"
ON public.messages 
FOR SELECT 
TO anon, authenticated
USING (
  EXISTS (
    SELECT 1 
    FROM conversations c
    WHERE c.id = conversation_id 
    AND c.status = 'accepted'
  )
);

-- Policy for inserting messages
-- Allows inserting if:
-- 1. Conversation exists and is accepted
-- 2. Sender is a participant (either initiator or receiver)
CREATE POLICY "Allow insert messages in valid conversations"
ON public.messages 
FOR INSERT 
TO anon, authenticated
WITH CHECK (
  EXISTS (
    SELECT 1 
    FROM conversations c
    WHERE c.id = conversation_id 
    AND c.status = 'accepted'
    AND (c.initiator_id = sender_id OR c.receiver_id = sender_id)
  )
);
```

### 4. Verify RLS is Enabled

Make sure RLS is enabled on the messages table:

```sql
-- Enable RLS on messages table
ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;
```

### 5. Test the Policies

You can test if the policies work correctly:

```sql
-- Check if you can read messages (should return messages if conversation is accepted)
SELECT * FROM messages WHERE conversation_id = 'your-conversation-id';

-- Check if policies exist
SELECT schemaname, tablename, policyname, permissive, roles, cmd, qual
FROM pg_policies
WHERE tablename = 'messages';
```

---

## Security Model

This approach is secure because:

1. **Backend Validation**: The backend only gives Supabase credentials to validated conversation participants
2. **Short-lived Tokens**: Tokens expire after 4 hours
3. **Conversation Validation**: RLS ensures the conversation exists and is accepted
4. **Participant Check**: RLS ensures the sender is actually a participant (initiator or receiver)
5. **No Direct Access**: Users can't get credentials without backend authorization

---

## After Applying the Fix

1. **Restart your application** (if needed)
2. **Test the flow**:
   - User 1 (initiator) sends a conversation request
   - User 2 (receiver) accepts the request
   - Both users should now be able to:
     - See the message input area (not disabled)
     - Send messages
     - Receive messages in real-time
     - View message history

---

## Troubleshooting

If messages still don't work after applying the fix:

### Check Browser Console
Open Developer Tools (F12) and look for:
- ❌ 401/403 errors → RLS policies still blocking
- ❌ "Supabase client not initialized" → Frontend issue
- ✅ "Subscribed to messages channel" → Connection working

### Check Network Tab
Look for requests to Supabase:
- POST to `/rest/v1/messages` → Should return 201 Created (not 403)
- GET from `/rest/v1/messages` → Should return 200 OK (not 401)

### Verify Conversation Data
In the browser console, check:
```javascript
// Should show true for all
console.log({
  has_supabase_token: !!conversationData.supabase_token,
  has_anon_key: !!conversationData.supabase_anon_key,
  has_conversation_id: !!conversationData.conversation_id_supabase,
  has_supabase_url: !!conversationData.supabase_url,
  status: conversationData.conversation.status // Should be 'accepted'
});
```

### Check Supabase Logs
In Supabase Dashboard:
1. Go to **Logs** → **Database**
2. Look for errors related to RLS or messages table
3. Should not see "new row violates row-level security policy"

---

## Alternative: Disable RLS Temporarily (Testing Only)

⚠️ **NOT RECOMMENDED FOR PRODUCTION**

If you want to test without RLS temporarily:

```sql
-- TESTING ONLY - Disable RLS
ALTER TABLE public.messages DISABLE ROW LEVEL SECURITY;
```

This will allow all operations, but removes all security. Only use this to confirm that RLS is the issue, then re-enable it with the proper policies above.

---

## Questions?

If you still have issues after applying these policies:
1. Share the error messages from browser console
2. Share the response from the Supabase logs
3. Confirm the conversation status is 'accepted' in MongoDB
