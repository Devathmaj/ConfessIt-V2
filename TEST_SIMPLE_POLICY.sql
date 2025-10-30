-- ============================================
-- SIMPLIFIED INSERT POLICY FOR TESTING
-- ============================================
-- This removes all conditions to test if the issue is with
-- the policy logic or with anon role permissions

-- Drop the complex policy
DROP POLICY IF EXISTS "Insert messages with validation" ON messages;

-- Create a simple policy that allows all inserts (for testing only!)
CREATE POLICY "Test insert policy - allow all"
ON public.messages
FOR INSERT
TO anon, authenticated
WITH CHECK (true);

-- Now try sending a message from your app
-- If this works, the issue is with the policy conditions
-- If this still fails, the issue is with anon role permissions

-- To restore the secure policy after testing, run SUPABASE_SECURE_FINAL.sql again
   