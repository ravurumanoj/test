# Unique SDK ChatCompletion API Fix

## Problem
When calling the `/relationship-manager/query` endpoint, the application was throwing:
```
ERROR: Application error: The installed unique-sdk ChatCompletion.create signature is incompatible with the expected messages format.
```

## Root Cause
The installed `unique-sdk` version (2026.30.1) has a different API signature than expected:
- **Expected**: `ChatCompletion.create(messages=..., model=..., tools=..., company_id=..., user_id=...)`
- **Actual**: `ChatCompletion.create(company_id, user_id, **params)`

The `_filter_supported_arguments()` method was inspecting the actual signature and filtering out parameters like `messages` because they weren't direct parameters - they need to be passed in the `**params` dict.

## Solution
Updated `src/app/services/unique_client.py` `create_completion()` method to:

1. **Build an options dict** for tools-related parameters:
   ```python
   options = {
       "tools": tools,
       "tool_choice": tool_choice,
       "temperature": temperature,
   }
   ```

2. **Build a params dict** with company_id, user_id, model, messages, and options:
   ```python
   params = {
       "company_id": self.settings.unique_auth_company_id,
       "user_id": self.settings.unique_auth_user_id,
       "model": self.settings.unique_model_name,
       "messages": messages,
       "options": options,  # contains tools, tool_choice, temperature
   }
   ```

3. **Call the SDK** with the correct signature:
   ```python
   result = create(**params)
   ```

## Files Changed
- `src/app/services/unique_client.py`: Updated `create_completion()` method (lines 48-117)

## Testing
The fix ensures that:
- ✅ Messages parameter is properly passed to the SDK
- ✅ Tool definitions and tool_choice are nested in the options dict
- ✅ Temperature and other parameters are correctly formatted
- ✅ The error "messages is not in payload" no longer occurs

## Validation
The application should now successfully:
1. Start the FastAPI server without errors
2. Accept POST requests to `/relationship-manager/query`
3. Call `UniqueToolkit.plan_with_tools()` without signature mismatch errors
4. Execute agent completions with tools enabled
