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
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
      "@typescript-eslint/no-unused-vars": "off",
      "no-control-regex": "error",
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
