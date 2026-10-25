import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      // Keep the classic rules-of-hooks checks. `reactHooks.configs.recommended`
      // in v7 also enables React Compiler-only rules (set-state-in-effect, refs,
      // immutability, purity, globals). This project does not run the compiler,
      // and those rules flag long-standing, intentional patterns across the app.
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-refresh/only-export-components": [
        "warn",
        {
          allowConstantExport: true,
          // These files deliberately co-locate a component with helpers, variants or
          // contexts that belong to it. Moving them out would only split the API and
          // update every importer; Fast Refresh handles the named exports fine.
          allowExportNames: [
            "AuthContext",
            "useAuth",
            "GamesConfigContext",
            "useGamesConfig",
            "FLORAL_PAIRS",
            "badgeVariants",
            "buttonVariants",
            "toggleVariants",
            "useFormField",
            "navigationMenuTriggerStyle",
            "useSidebar",
            "toast",
            "EXPIRED_MATCH_NOTICE",
            "UNANSWERED_COOLDOWN",
            "readCooldown",
            "readCohortBlock",
            "cooldownPermitsMatch",
            "describeExpiredMatch",
            "formatRemaining",
            "NO_CANDIDATES",
            "COHORT_NOT_CONFIGURED",
            "readDrawRefusal",
            "describeDrawRefusal",
            "timeUntilNextMatch",
            "notifyExpiredMatch",
            "cooldownNoticeSuppressed",
            "notifyCooldown",
            "isExpired",
            "conversationIsTerminated",
            "conversationIsExpired",
            "matchesConversationFilter",
          ],
        },
      ],
      "@typescript-eslint/no-unused-vars": "off",
      "no-control-regex": "error",
      // Warn, not error: production stripping is enforced by the esbuild `drop`
      // above, so this is a nudge against adding new call sites rather than a
      // gate on the hundred historical ones. New code should log through no
      // channel at all, or behind `import.meta.env.DEV`.
      "no-console": "warn",
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-empty-object-type": "warn",
      "prefer-const": "warn",
      "@typescript-eslint/no-require-imports": "warn"
    },
  },
  {
    // Turned off for one file, and only that file. src/lib/limits.ts exists to say which
    // characters are not allowed, so a rule that objects to control characters inside a regular
    // expression there is objecting to the point of the module. Everywhere else the rule stays on,
    // because a stray control character in some unrelated regex is a real bug.
    files: ["src/lib/limits.ts"],
    rules: {
      "no-control-regex": "off",
    },
  }
);
