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


def unregister():
    """Unregister all operators."""
    # Lazy imports - these are only executed when unregister() is called
    from . import pack_ops
    from . import submit_ops
    
    submit_ops.unregister()
    pack_ops.unregister()
