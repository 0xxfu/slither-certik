import logging
from typing import Union, List, TYPE_CHECKING

from slither.core.declarations import (
    Function,
    SolidityVariable,
    SolidityVariableComposed,
    SolidityFunction,
    Contract,
)
from slither.core.declarations.enum import Enum
from slither.core.expressions import (
    AssignmentOperation,
    AssignmentOperationType,
    UnaryOperationType,
    BinaryOperationType,
    ElementaryTypeNameExpression,
    CallExpression,
    Identifier,
    MemberAccess,
)
from slither.core.expressions.binary_operation import BinaryOperation
from slither.core.expressions.expression import Expression
from slither.core.expressions.index_access import IndexAccess
from slither.core.expressions.literal import Literal
from slither.core.expressions.new_array import NewArray
from slither.core.expressions.new_contract import NewContract
from slither.core.expressions.tuple_expression import TupleExpression
from slither.core.expressions.unary_operation import UnaryOperation
from slither.core.solidity_types import ArrayType, ElementaryType, TypeAlias
from slither.core.solidity_types.type import Type
from slither.core.variables.local_variable import LocalVariable
from slither.core.variables.local_variable_init_from_tuple import LocalVariableInitFromTuple
from slither.core.variables.state_variable import StateVariable
from slither.core.variables.variable import Variable
from slither.slithir.exceptions import SlithIRError
from slither.slithir.operations import (
    Assignment,
    Binary,
    BinaryType,
    Delete,
    Index,
    InitArray,
    InternalCall,
    Member,
    TypeConversion,
    Unary,
    Unpack,
    Return,
    SolidityCall,
    Operation,
)
from slither.slithir.tmp_operations.argument import Argument
from slither.slithir.tmp_operations.tmp_call import TmpCall
from slither.slithir.tmp_operations.tmp_new_array import TmpNewArray
from slither.slithir.tmp_operations.tmp_new_contract import TmpNewContract
from slither.slithir.tmp_operations.tmp_new_elementary_type import TmpNewElementaryType
from slither.slithir.variables import (
    Constant,
    ReferenceVariable,
    TemporaryVariable,
    TupleVariable,
)
from slither.visitors.expression.expression import ExpressionVisitor
from slither.visitors.expression.constants_folding import ConstantFolding, NotConstant

if TYPE_CHECKING:
    from slither.core.cfg.node import Node

logger = logging.getLogger("VISTIOR:ExpressionToSlithIR")

key = "expressionToSlithIR"


def get(expression: Union[Expression, Operation]):
    val = expression.context[key]
    # we delete the item to reduce memory use
    del expression.context[key]
    return val


def get_without_removing(expression):
    return expression.context[key]


def set_val(expression: Union[Expression, Operation], val) -> None:
    expression.context[key] = val


_binary_to_binary = {
    BinaryOperationType.POWER: BinaryType.POWER,
    BinaryOperationType.MULTIPLICATION: BinaryType.MULTIPLICATION,
    BinaryOperationType.DIVISION: BinaryType.DIVISION,
    BinaryOperationType.MODULO: BinaryType.MODULO,
    BinaryOperationType.ADDITION: BinaryType.ADDITION,
    BinaryOperationType.SUBTRACTION: BinaryType.SUBTRACTION,
    BinaryOperationType.LEFT_SHIFT: BinaryType.LEFT_SHIFT,
    BinaryOperationType.RIGHT_SHIFT: BinaryType.RIGHT_SHIFT,
    BinaryOperationType.AND: BinaryType.AND,
    BinaryOperationType.CARET: BinaryType.CARET,
    BinaryOperationType.OR: BinaryType.OR,
    BinaryOperationType.LESS: BinaryType.LESS,
    BinaryOperationType.GREATER: BinaryType.GREATER,
    BinaryOperationType.LESS_EQUAL: BinaryType.LESS_EQUAL,
    BinaryOperationType.GREATER_EQUAL: BinaryType.GREATER_EQUAL,
    BinaryOperationType.EQUAL: BinaryType.EQUAL,
    BinaryOperationType.NOT_EQUAL: BinaryType.NOT_EQUAL,
    BinaryOperationType.ANDAND: BinaryType.ANDAND,
    BinaryOperationType.OROR: BinaryType.OROR,
}

_signed_to_unsigned = {
    BinaryOperationType.DIVISION_SIGNED: BinaryType.DIVISION,
    BinaryOperationType.MODULO_SIGNED: BinaryType.MODULO,
    BinaryOperationType.LESS_SIGNED: BinaryType.LESS,
    BinaryOperationType.GREATER_SIGNED: BinaryType.GREATER,
    BinaryOperationType.RIGHT_SHIFT_ARITHMETIC: BinaryType.RIGHT_SHIFT,
}


def convert_assignment(
    left: Union[LocalVariable, StateVariable, ReferenceVariable],
    right: Union[LocalVariable, StateVariable, ReferenceVariable],
    t: AssignmentOperationType,
    return_type,
) -> Union[Binary, Assignment]:
    if t == AssignmentOperationType.ASSIGN:
        return Assignment(left, right, return_type)
    if t == AssignmentOperationType.ASSIGN_OR:
        return Binary(left, left, right, BinaryType.OR)
    if t == AssignmentOperationType.ASSIGN_CARET:
        return Binary(left, left, right, BinaryType.CARET)
    if t == AssignmentOperationType.ASSIGN_AND:
        return Binary(left, left, right, BinaryType.AND)
    if t == AssignmentOperationType.ASSIGN_LEFT_SHIFT:
        return Binary(left, left, right, BinaryType.LEFT_SHIFT)
    if t == AssignmentOperationType.ASSIGN_RIGHT_SHIFT:
        return Binary(left, left, right, BinaryType.RIGHT_SHIFT)
    if t == AssignmentOperationType.ASSIGN_ADDITION:
        return Binary(left, left, right, BinaryType.ADDITION)
    if t == AssignmentOperationType.ASSIGN_SUBTRACTION:
        return Binary(left, left, right, BinaryType.SUBTRACTION)
    if t == AssignmentOperationType.ASSIGN_MULTIPLICATION:
        return Binary(left, left, right, BinaryType.MULTIPLICATION)
    if t == AssignmentOperationType.ASSIGN_DIVISION:
        return Binary(left, left, right, BinaryType.DIVISION)
    if t == AssignmentOperationType.ASSIGN_MODULO:
        return Binary(left, left, right, BinaryType.MODULO)

    raise SlithIRError("Missing type during assignment conversion")


class ExpressionToSlithIR(ExpressionVisitor):
    # pylint: disable=super-init-not-called
    def __init__(self, expression: Expression, node: "Node") -> None:
        from slither.core.cfg.node import NodeType  # pylint: disable=import-outside-toplevel

        self._expression = expression
        self._node = node
        self._result: List[Operation] = []
        self._visit_expression(self.expression)
        if node.type == NodeType.RETURN:
            r = Return(get(self.expression))
            r.set_expression(expression)
            self._result.append(r)
        for ir in self._result:
            ir.set_node(node)

    def result(self) -> List[Operation]:
        return self._result

    def _post_assignement_operation(self, expression: AssignmentOperation) -> None:
        left = get(expression.expression_left)
        right = get(expression.expression_right)
        if isinstance(left, list):  # tuple expression:
            if isinstance(right, list):  # unbox assigment
                assert len(left) == len(right)
                for idx, _ in enumerate(left):
                    if not left[idx] is None:
                        operation = convert_assignment(
                            left[idx],
                            right[idx],
                            expression.type,
                            expression.expression_return_type,
                        )
                        operation.set_expression(expression)
                        self._result.append(operation)
                set_val(expression, None)
            else:
                assert isinstance(right, TupleVariable)
                for idx, _ in enumerate(left):
                    if not left[idx] is None:
                        index = idx
                        # The following test is probably always true?
                        if (
                            isinstance(left[idx], LocalVariableInitFromTuple)
                            and left[idx].tuple_index is not None
                        ):
                            index = left[idx].tuple_index
                        operation = Unpack(left[idx], right, index)
                        operation.set_expression(expression)
                        self._result.append(operation)
                set_val(expression, None)
        # Tuple with only one element. We need to convert the assignment to a Unpack
        # Ex:
        # (uint a,,) = g()
        elif (
            isinstance(left, LocalVariableInitFromTuple)
            and left.tuple_index is not None
            and isinstance(right, TupleVariable)
        ):
            operation = Unpack(left, right, left.tuple_index)
            operation.set_expression(expression)
            self._result.append(operation)
            set_val(expression, None)
        else:
            # Init of array, like
            # uint8[2] var = [1,2];
            if isinstance(right, list):
                operation = InitArray(right, left)
                operation.set_expression(expression)
                self._result.append(operation)
                set_val(expression, left)
            # Disabled https://github.com/crytic/slither/pull/1761 here.
            # We prefer https://github.com/CertiKProject/slither-certik/pull/26.
            # elif isinstance(left.type, ArrayType):
            #     # Special case for init of array, when the right has only one element
            #     operation = InitArray([right], left)
            #     operation.set_expression(expression)
            #     self._result.append(operation)
            #     set_val(expression, left)
            else:
                operation = convert_assignment(
                    left, right, expression.type, expression.expression_return_type
                )
                operation.set_expression(expression)
                self._result.append(operation)
                # Return left to handle
                # a = b = 1;
                set_val(expression, left)

    def _attempt_constant_folding(self, expression):
        try:
            const_fold = ConstantFolding(expression, expression.type)
        except (NotConstant, AttributeError):
            return False

        const_value = const_fold.result()

        # Slither's Expression AST doesn't propagate type information.
        # expression.type holds the kind of binary/unary operation, not the Solidity type.
        # So, we don't have actual Solidity type here and need to guess the type for the Constant value.
        if expression.type in _signed_to_unsigned:
            new_type = ElementaryType("uint")
        elif isinstance(const_value.value, bool):
            new_type = ElementaryType("bool")
        elif isinstance(const_value.value, int):
            new_type = ElementaryType("int")
        else:
            new_type = ElementaryType("string")
        cst = Constant(str(const_value.value), new_type)
        set_val(expression, cst)
        return True

    def _post_binary_operation(self, expression: BinaryOperation) -> None:
        if self._node.compilation_unit.generates_certik_ir and self._attempt_constant_folding(expression):
            return

        left = get(expression.expression_left)
        right = get(expression.expression_right)
        val = TemporaryVariable(self._node)

        if expression.type in _signed_to_unsigned:
            new_left = TemporaryVariable(self._node)
            conv_left = TypeConversion(new_left, left, ElementaryType("int256"))
            new_left.set_type(ElementaryType("int256"))
            conv_left.set_expression(expression)
            self._result.append(conv_left)

            if expression.type != BinaryOperationType.RIGHT_SHIFT_ARITHMETIC:
                new_right = TemporaryVariable(self._node)
                conv_right = TypeConversion(new_right, right, ElementaryType("int256"))
                new_right.set_type(ElementaryType("int256"))
                conv_right.set_expression(expression)
                self._result.append(conv_right)
            else:
                new_right = right

            new_final = TemporaryVariable(self._node)
            operation = Binary(new_final, new_left, new_right, _signed_to_unsigned[expression.type])
            operation.set_expression(expression)
            self._result.append(operation)

            conv_final = TypeConversion(val, new_final, ElementaryType("uint256"))
            val.set_type(ElementaryType("uint256"))
            conv_final.set_expression(expression)
            self._result.append(conv_final)
        else:
            operation = Binary(val, left, right, _binary_to_binary[expression.type])
            operation.set_expression(expression)
            self._result.append(operation)

        set_val(expression, val)

    # pylint: disable=too-many-branches,too-many-statements,too-many-locals
    def _post_call_expression(self, expression: CallExpression) -> None:

        assert isinstance(expression, CallExpression)

        expression_called = expression.called
        called = get(expression_called)

        args = [get(a) for a in expression.arguments if a]
        for arg in args:
            arg_ = Argument(arg)
            arg_.set_expression(expression)
            self._result.append(arg_)
        if isinstance(called, Function):
            # internal call

            # If tuple
            if expression.type_call.startswith("tuple(") and expression.type_call != "tuple()":
                val = TupleVariable(self._node)
            else:
                assert len(called.returns) <= 1
                val = TemporaryVariable(
                    self._node,
                    location = called.returns[0].location if len(called.returns) == 1 else None
                )
            internal_call = InternalCall(called, len(args), val, expression.type_call)
            internal_call.set_expression(expression)
            self._result.append(internal_call)
            set_val(expression, val)

        # User defined types
        elif (
            isinstance(called, TypeAlias)
            and isinstance(expression_called, MemberAccess)
            and expression_called.member_name in ["wrap", "unwrap"]
            and len(args) == 1
        ):
            # wrap: underlying_type -> alias
            # unwrap: alias -> underlying_type
            dest_type = (
                called if expression_called.member_name == "wrap" else called.underlying_type
            )
            val = TemporaryVariable(self._node)
            var = TypeConversion(val, args[0], dest_type)
            var.set_expression(expression)
            val.set_type(dest_type)
            self._result.append(var)
            set_val(expression, val)

        # yul things
        elif called.name == "caller()":
            val = TemporaryVariable(self._node)
            var = Assignment(val, SolidityVariableComposed("msg.sender"), "uint256")
            self._result.append(var)
            set_val(expression, val)
        elif called.name == "origin()":
            val = TemporaryVariable(self._node)
            var = Assignment(val, SolidityVariableComposed("tx.origin"), "uint256")
            self._result.append(var)
            set_val(expression, val)
        elif called.name == "extcodesize(uint256)":
            val = ReferenceVariable(self._node)
            var = Member(args[0], Constant("codesize"), val)
            self._result.append(var)
            set_val(expression, val)
        elif called.name == "selfbalance()":
            val = TemporaryVariable(self._node)
            var = TypeConversion(val, SolidityVariable("this"), ElementaryType("address"))
            val.set_type(ElementaryType("address"))
            self._result.append(var)

            val1 = ReferenceVariable(self._node)
            var1 = Member(val, Constant("balance"), val1)
            self._result.append(var1)
            set_val(expression, val1)
        elif called.name == "address()":
            val = TemporaryVariable(self._node)
            var = TypeConversion(val, SolidityVariable("this"), ElementaryType("address"))
            val.set_type(ElementaryType("address"))
            self._result.append(var)
            set_val(expression, val)
        elif called.name == "callvalue()":
            val = TemporaryVariable(self._node)
            var = Assignment(val, SolidityVariableComposed("msg.value"), "uint256")
            self._result.append(var)
            set_val(expression, val)

        else:
            # If tuple
            if expression.type_call.startswith("tuple(") and expression.type_call != "tuple()":
                val = TupleVariable(self._node)
            else:
                val = TemporaryVariable(self._node)

            message_call = TmpCall(called, len(args), val, expression.type_call)
            message_call.set_expression(expression)
            # Gas/value are only accessible here if the syntax {gas: , value: }
            # Is used over .gas().value()
            if expression.call_gas:
                call_gas = get(expression.call_gas)
                message_call.call_gas = call_gas
            if expression.call_value:
                call_value = get(expression.call_value)
                message_call.call_value = call_value
            if expression.call_salt:
                call_salt = get(expression.call_salt)
                message_call.call_salt = call_salt
            self._result.append(message_call)
            set_val(expression, val)

    def _post_conditional_expression(self, expression):
        raise Exception(f"Ternary operator are not convertible to SlithIR {expression}")

    def _post_elementary_type_name_expression(
        self,
        expression: ElementaryTypeNameExpression,
    ) -> None:
        set_val(expression, expression.type)

    def _post_identifier(self, expression: Identifier) -> None:
        set_val(expression, expression.value)

    def _post_index_access(self, expression: IndexAccess) -> None:
        left = get(expression.expression_left)
        right = get(expression.expression_right)
        # Left can be a type for abi.decode(var, uint[2])
        if isinstance(left, Type):
            # Nested type are not yet supported by abi.decode, so the assumption
            # Is that the right variable must be a constant
            assert isinstance(right, Constant)
            t = ArrayType(left, right.value)
            set_val(expression, t)
            return
        val = ReferenceVariable(self._node)
        # access to anonymous array
        # such as [0,1][x]
        if isinstance(left, list):
            init_array_val = TemporaryVariable(self._node)
            init_array_right = left
            left = init_array_val
            operation = InitArray(init_array_right, init_array_val)
            operation.set_expression(expression)
            self._result.append(operation)
        operation = Index(val, left, right, expression.type)
        operation.set_expression(expression)
        self._result.append(operation)
        set_val(expression, val)

    def _post_literal(self, expression: Literal) -> None:
        cst = Constant(expression.value, expression.type, expression.subdenomination)
        set_val(expression, cst)

    def _post_member_access(self, expression: MemberAccess) -> None:
        expr = get(expression.expression)

        # Look for type(X).max / min
        # Because we looked at the AST structure, we need to look into the nested expression
        # Hopefully this is always on a direct sub field, and there is no weird construction
        if isinstance(expression.expression, CallExpression) and expression.member_name in [
            "min",
            "max",
        ]:
            if isinstance(expression.expression.called, Identifier):
                if expression.expression.called.value == SolidityFunction("type()"):
                    assert len(expression.expression.arguments) == 1
                    val = TemporaryVariable(self._node)
                    type_expression_found = expression.expression.arguments[0]
                    if isinstance(type_expression_found, ElementaryTypeNameExpression):
                        type_found = type_expression_found.type
                        constant_type = type_found
                    else:
                        # type(enum).max/min
                        assert isinstance(type_expression_found, Identifier)
                        type_found = type_expression_found.value
                        assert isinstance(type_found, Enum)
                        constant_type = None
                    if expression.member_name == "min":
                        op = Assignment(
                            val,
                            Constant(str(type_found.min), constant_type),
                            type_found,
                        )
                    else:
                        op = Assignment(
                            val,
                            Constant(str(type_found.max), constant_type),
                            type_found,
                        )
                    self._result.append(op)
                    set_val(expression, val)
                    return

        # This does not support solidity 0.4 contract_name.balance
        if (
            isinstance(expr, Variable)
            and expr.type == ElementaryType("address")
            and expression.member_name in ["balance", "code", "codehash"]
        ):
            val = TemporaryVariable(self._node)
            name = expression.member_name + "(address)"
            sol_func = SolidityFunction(name)
            s = SolidityCall(
                sol_func,
                1,
                val,
                sol_func.return_type,
            )
            s.set_expression(expression)
            s.arguments.append(expr)
            self._result.append(s)
            set_val(expression, val)
            return

        if isinstance(expr, TypeAlias) and expression.member_name in ["wrap", "unwrap"]:
            # The logic is be handled by _post_call_expression
            set_val(expression, expr)
            return

        if isinstance(expr, Contract):
            # Early lookup to detect user defined types from other contracts definitions
            # contract A { type MyInt is int}
            # contract B { function f() public{ A.MyInt test = A.MyInt.wrap(1);}}
            # The logic is handled by _post_call_expression
            if expression.member_name in expr.file_scope.user_defined_types:
                set_val(expression, expr.file_scope.user_defined_types[expression.member_name])
                return
            # Lookup errors referred to as member of contract e.g. Test.myError.selector
            if expression.member_name in expr.custom_errors_as_dict:
                set_val(expression, expr.custom_errors_as_dict[expression.member_name])
                return

        val = ReferenceVariable(self._node)
        member = Member(expr, Constant(expression.member_name), val)
        member.set_expression(expression)
        self._result.append(member)
        set_val(expression, val)

    def _post_new_array(self, expression: NewArray) -> None:
        val = TemporaryVariable(self._node)
        operation = TmpNewArray(expression.depth, expression.array_type, val)
        operation.set_expression(expression)
        self._result.append(operation)
        set_val(expression, val)

    def _post_new_contract(self, expression: NewContract) -> None:
        val = TemporaryVariable(self._node)
        operation = TmpNewContract(expression.contract_name, val)
        operation.set_expression(expression)
        if expression.call_value:
            call_value = get(expression.call_value)
            operation.call_value = call_value
        if expression.call_salt:
            call_salt = get(expression.call_salt)
            operation.call_salt = call_salt

        self._result.append(operation)
        set_val(expression, val)

    def _post_new_elementary_type(self, expression):
        # TODO unclear if this is ever used?
        val = TemporaryVariable(self._node)
        operation = TmpNewElementaryType(expression.type, val)
        operation.set_expression(expression)
        self._result.append(operation)
        set_val(expression, val)

    def _post_tuple_expression(self, expression: TupleExpression) -> None:
        expressions = [get(e) if e else None for e in expression.expressions]
        if expression.is_inline_array:
            temp_var = TemporaryVariable(self._node)
            init_arr = InitArray(expressions, temp_var)
            self._result.append(init_arr)
            # Use the new temporary variable in place of the array.
            val = temp_var
        elif len(expressions) == 1:
            val = expressions[0]
        else:
            val = expressions
        set_val(expression, val)

    def _post_type_conversion(self, expression: TypeConversion) -> None:
        expr = get(expression.expression)
        val = TemporaryVariable(self._node)
        operation = TypeConversion(val, expr, expression.type)
        val.set_type(expression.type)
        operation.set_expression(expression)
        self._result.append(operation)
        set_val(expression, val)

    # pylint: disable=too-many-statements
    def _post_unary_operation(self, expression: UnaryOperation) -> None:
        if self._node.compilation_unit.generates_certik_ir and self._attempt_constant_folding(expression):
            return

        value = get(expression.expression)
        if expression.type in [UnaryOperationType.BANG, UnaryOperationType.TILD]:
            lvalue = TemporaryVariable(self._node)
            operation = Unary(lvalue, value, expression.type)
            operation.set_expression(expression)
            self._result.append(operation)
            set_val(expression, lvalue)
        elif expression.type in [UnaryOperationType.DELETE]:
            operation = Delete(value, value)
            operation.set_expression(expression)
            self._result.append(operation)
            set_val(expression, value)
        elif expression.type in [UnaryOperationType.PLUSPLUS_PRE]:
            operation = Binary(value, value, Constant("1", value.type), BinaryType.ADDITION)
            operation.set_expression(expression)
            self._result.append(operation)
            set_val(expression, value)
        elif expression.type in [UnaryOperationType.MINUSMINUS_PRE]:
            operation = Binary(value, value, Constant("1", value.type), BinaryType.SUBTRACTION)
            operation.set_expression(expression)
            self._result.append(operation)
            set_val(expression, value)
        elif expression.type in [UnaryOperationType.PLUSPLUS_POST]:
            lvalue = TemporaryVariable(self._node)
            operation = Assignment(lvalue, value, value.type)
            operation.set_expression(expression)
            self._result.append(operation)
            operation = Binary(value, value, Constant("1", value.type), BinaryType.ADDITION)
            operation.set_expression(expression)
            self._result.append(operation)
            set_val(expression, lvalue)
        elif expression.type in [UnaryOperationType.MINUSMINUS_POST]:
            lvalue = TemporaryVariable(self._node)
            operation = Assignment(lvalue, value, value.type)
            operation.set_expression(expression)
            self._result.append(operation)
            operation = Binary(value, value, Constant("1", value.type), BinaryType.SUBTRACTION)
            operation.set_expression(expression)
            self._result.append(operation)
            set_val(expression, lvalue)
        elif expression.type in [UnaryOperationType.PLUS_PRE]:
            set_val(expression, value)
        elif expression.type in [UnaryOperationType.MINUS_PRE]:
            lvalue = TemporaryVariable(self._node)
            operation = Binary(lvalue, Constant("0", value.type), value, BinaryType.SUBTRACTION)
            operation.set_expression(expression)
            self._result.append(operation)
            set_val(expression, lvalue)
        else:
            raise SlithIRError(f"Unary operation to IR not supported {expression}")
