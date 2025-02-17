from mypy.types import Instance, get_proper_type
from typing import DefaultDict, Dict, List, NamedTuple, Set, Optional, Tuple
from collections import defaultdict

from mypy.nodes import (
    Decorator, Expression, FuncDef, FuncItem, LambdaExpr, NameExpr, SymbolNode, Var, MemberExpr,
    CallExpr, RefExpr, TypeInfo
)
from mypy.traverser import TraverserVisitor


class PreBuildVisitor(TraverserVisitor):
    """Mypy file AST visitor run before building the IR.

    This collects various things, including:

    * Determine relationships between nested functions and functions that
      contain nested functions
    * Find non-local variables (free variables)
    * Find property setters
    * Find decorators of functions

    The main IR build pass uses this information.
    """

    def __init__(self) -> None:
        super().__init__()
        # Dict from a function to symbols defined directly in the
        # function that are used as non-local (free) variables within a
        # nested function.
        self.free_variables: Dict[FuncItem, Set[SymbolNode]] = {}

        # Intermediate data structure used to find the function where
        # a SymbolNode is declared. Initially this may point to a
        # function nested inside the function with the declaration,
        # but we'll eventually update this to refer to the function
        # with the declaration.
        self.symbols_to_funcs: Dict[SymbolNode, FuncItem] = {}

        # Stack representing current function nesting.
        self.funcs: List[FuncItem] = []

        # All property setters encountered so far.
        self.prop_setters: Set[FuncDef] = set()

        # A map from any function that contains nested functions to
        # a set of all the functions that are nested within it.
        self.encapsulating_funcs: Dict[FuncItem, List[FuncItem]] = {}

        # Map nested function to its parent/encapsulating function.
        self.nested_funcs: Dict[FuncItem, FuncItem] = {}

        # Map function to its non-special decorators.
        self.funcs_to_decorators: Dict[FuncDef, List[Expression]] = {}

        # Map of main singledispatch function to list of registered implementations
        self.singledispatch_impls: DefaultDict[
            FuncDef, List[Tuple[TypeInfo, FuncDef]]] = defaultdict(list)

    def visit_decorator(self, dec: Decorator) -> None:
        if dec.decorators:
            # Only add the function being decorated if there exist
            # (ordinary) decorators in the decorator list. Certain
            # decorators (such as @property, @abstractmethod) are
            # special cased and removed from this list by
            # mypy. Functions decorated only by special decorators
            # (and property setters) are not treated as decorated
            # functions by the IR builder.
            if isinstance(dec.decorators[0], MemberExpr) and dec.decorators[0].name == 'setter':
                # Property setters are not treated as decorated methods.
                self.prop_setters.add(dec.func)
            else:
                removed: List[int] = []
                for i, d in enumerate(dec.decorators):
                    impl = get_singledispatch_register_call_info(d, dec.func)
                    if impl is not None:
                        self.singledispatch_impls[impl.singledispatch_func].append(
                            (impl.dispatch_type, dec.func))
                        removed.append(i)
                for i in reversed(removed):
                    del dec.decorators[i]
                # if the only decorators are register calls, we shouldn't treat this
                # as a decorated function because there aren't any decorators to apply
                if not dec.decorators:
                    return

                self.funcs_to_decorators[dec.func] = dec.decorators
        super().visit_decorator(dec)

    def visit_func_def(self, fdef: FuncItem) -> None:
        # TODO: What about overloaded functions?
        self.visit_func(fdef)

    def visit_lambda_expr(self, expr: LambdaExpr) -> None:
        self.visit_func(expr)

    def visit_func(self, func: FuncItem) -> None:
        # If there were already functions or lambda expressions
        # defined in the function stack, then note the previous
        # FuncItem as containing a nested function and the current
        # FuncItem as being a nested function.
        if self.funcs:
            # Add the new func to the set of nested funcs within the
            # func at top of the func stack.
            self.encapsulating_funcs.setdefault(self.funcs[-1], []).append(func)
            # Add the func at top of the func stack as the parent of
            # new func.
            self.nested_funcs[func] = self.funcs[-1]

        self.funcs.append(func)
        super().visit_func(func)
        self.funcs.pop()

    def visit_name_expr(self, expr: NameExpr) -> None:
        if isinstance(expr.node, (Var, FuncDef)):
            self.visit_symbol_node(expr.node)

    def visit_var(self, var: Var) -> None:
        self.visit_symbol_node(var)

    def visit_symbol_node(self, symbol: SymbolNode) -> None:
        if not self.funcs:
            # We are not inside a function and hence do not need to do
            # anything regarding free variables.
            return

        if symbol in self.symbols_to_funcs:
            orig_func = self.symbols_to_funcs[symbol]
            if self.is_parent(self.funcs[-1], orig_func):
                # The function in which the symbol was previously seen is
                # nested within the function currently being visited. Thus
                # the current function is a better candidate to contain the
                # declaration.
                self.symbols_to_funcs[symbol] = self.funcs[-1]
                # TODO: Remove from the orig_func free_variables set?
                self.free_variables.setdefault(self.funcs[-1], set()).add(symbol)

            elif self.is_parent(orig_func, self.funcs[-1]):
                # The SymbolNode instance has already been visited
                # before in a parent function, thus it's a non-local
                # symbol.
                self.add_free_variable(symbol)

        else:
            # This is the first time the SymbolNode is being
            # visited. We map the SymbolNode to the current FuncDef
            # being visited to note where it was first visited.
            self.symbols_to_funcs[symbol] = self.funcs[-1]

    def is_parent(self, fitem: FuncItem, child: FuncItem) -> bool:
        # Check if child is nested within fdef (possibly indirectly
        # within multiple nested functions).
        if child in self.nested_funcs:
            parent = self.nested_funcs[child]
            if parent == fitem:
                return True
            return self.is_parent(fitem, parent)
        return False

    def add_free_variable(self, symbol: SymbolNode) -> None:
        # Find the function where the symbol was (likely) first declared,
        # and mark is as a non-local symbol within that function.
        func = self.symbols_to_funcs[symbol]
        self.free_variables.setdefault(func, set()).add(symbol)


class RegisteredImpl(NamedTuple):
    singledispatch_func: FuncDef
    dispatch_type: TypeInfo


def get_singledispatch_register_call_info(decorator: Expression, func: FuncDef
                                          ) -> Optional[RegisteredImpl]:
    # @fun.register(complex)
    # def g(arg): ...
    if (isinstance(decorator, CallExpr) and len(decorator.args) == 1
            and isinstance(decorator.args[0], RefExpr)):
        callee = decorator.callee
        dispatch_type = decorator.args[0].node
        if not isinstance(dispatch_type, TypeInfo):
            return None

        if isinstance(callee, MemberExpr):
            return registered_impl_from_possible_register_call(callee, dispatch_type)
    # @fun.register
    # def g(arg: int): ...
    elif isinstance(decorator, MemberExpr):
        # we don't know if this is a register call yet, so we can't be sure that the function
        # actually has arguments
        if not func.arguments:
            return None
        arg_type = get_proper_type(func.arguments[0].variable.type)
        if not isinstance(arg_type, Instance):
            return None
        info = arg_type.type
        return registered_impl_from_possible_register_call(decorator, info)
    return None


def registered_impl_from_possible_register_call(expr: MemberExpr, dispatch_type: TypeInfo
                                                ) -> Optional[RegisteredImpl]:
    if expr.name == 'register' and isinstance(expr.expr, NameExpr):
        node = expr.expr.node
        if isinstance(node, Decorator):
            return RegisteredImpl(node.func, dispatch_type)
    return None
