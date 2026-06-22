# JavaScript / TypeScript / Angular / Node.js Commit Checks

Clean up and verify JavaScript/TypeScript/Angular/Node.js code integrity before committing:

1.  **Format**: Run the project's formatter.
    - Check `package.json` for a format script (e.g., `npm run format`).
    - Otherwise try standard tools like `prettier --write .`.
    - You must wait for the format command to finish before committing.
2.  **Lint**: Run the project's linter.
    - Check `package.json` for a lint script (e.g., `npm run lint`, `ng lint`).
    - Otherwise try `eslint .`.
3.  **Test**: Run the project's tests.
    - Check `package.json` for a test script (e.g., `npm run test:ci`, `ng test --watch=false`).
4.  **Build**: (Optional) Verify the build passes if a build script exists (e.g., `npm run build`, `ng build`).

Wait for all of these things to complete without errors before committing. If there
are errors, fix the errors and try again before committing.