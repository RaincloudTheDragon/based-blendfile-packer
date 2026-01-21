"""
Operators for SheepIt Project Submitter addon.
"""


def register():
    """Register all operators."""
    # Lazy imports - these are only executed when register() is called
    # This avoids circular import issues since imports happen at function call time
    from . import pack_ops
    from . import submit_ops
    
    pack_ops.register()
    submit_ops.register()
    
    # Import auth_ops separately with error handling
    try:
        from . import auth_ops
        auth_ops.register()
    except ImportError as e:
        import traceback
        print(f"[SheepIt] Warning: Could not import auth_ops: {e}")
        traceback.print_exc()
        # Continue without auth_ops - browser login features won't be available
    except Exception as e:
        import traceback
        print(f"[SheepIt] Error importing auth_ops: {e}")
        traceback.print_exc()


def unregister():
    """Unregister all operators."""
    # Lazy imports - these are only executed when unregister() is called
    from . import pack_ops
    from . import submit_ops
    
    submit_ops.unregister()
    pack_ops.unregister()
    
    # Try to unregister auth_ops if it was registered
    try:
        from . import auth_ops
        auth_ops.unregister()
    except (ImportError, Exception):
        pass  # Ignore errors during unregister
