"""One fixed read-only RabbitMQ 4.3.6 metadata RPC; no caller expressions."""

import subprocess
import tempfile
from pathlib import Path

from . import dependency_policy, operator_units
from .policy import DeliveryError, fields

CTL = Path("/usr/lib/rabbitmq/sbin/rabbitmqctl")
# Fixed getters only: no caller text, mutation, message reads or password hashes.
SNAPSHOT_EXPRESSION = r"""
rpc:call(node(), erlang, apply, [fun() ->
    Count = rabbit_amqqueue:count(<<"kf">>),
    true = (Count =:= 0 orelse Count =:= 1),
    Normalize = fun
        (name, {resource, <<"kf">>, queue, Value}) -> Value;
        (arguments, Value) -> [[K, atom_to_binary(T, utf8), V] || {K,T,V} <- Value];
        (type, rabbit_classic_queue) -> <<"classic">>;
        (K, Value) when K =:= policy; K =:= operator_policy ->
            case Value of undefined -> <<>>; none -> <<>>; '' -> <<>>; _ -> Value end;
        (effective_policy_definition, Value) when is_list(Value) -> maps:from_list(Value);
        (tags, Value) -> [case T of
            A when is_atom(A) -> atom_to_binary(A,utf8); _ -> T end || T <- Value];
        (_, Value) when is_atom(Value), Value =/= true, Value =/= false ->
            atom_to_binary(Value,utf8);
        (_, Value) -> Value
    end,
    ToMap = fun(Items) -> maps:from_list(
        [{atom_to_binary(K,utf8),Normalize(K,V)} || {K,V} <- Items]) end,
    Queues = [ToMap(Row) || Row <- rabbit_amqqueue:info_all(<<"kf">>,
        [name,durable,auto_delete,exclusive,type,state,arguments,messages_ready,
         messages_unacknowledged,message_bytes,consumers,policy,operator_policy,
         effective_policy_definition])],
    Limits = [#{<<"vhost">> => V,
                <<"limits">> => iolist_to_binary(rabbit_json:encode(maps:from_list(L)))}
              || {V,L} <- rabbit_vhost_limit:list()],
    Users = [ToMap(Row) || Row <- rabbit_auth_backend_internal:list_users()],
    Permissions = [ToMap(Row) || Row <-
        rabbit_auth_backend_internal:list_vhost_permissions(<<"kf">>)],
    iolist_to_binary(rabbit_json:encode(#{<<"queues">> => Queues, <<"vhost_limits">> => Limits,
        <<"users">> => Users, <<"permissions">> => Permissions}))
end, []], 5000).
""".strip()


def command() -> dict:
    env = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "HOME": dependency_policy.STORAGE["rabbitmq"]["path"],
        "ERL_EPMD_ADDRESS": "127.0.0.1",
        "RABBITMQ_NODENAME": "kf@localhost",
        "RABBITMQ_CTL_ERL_ARGS": (
            "+S 1:1 +A 2 +P 1024 +Q 1024 -kernel inet_dist_use_interface {127,0,0,1}"
        ),
    }
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                [
                    str(CTL),
                    "-q",
                    "-n",
                    "kf@localhost",
                    "--timeout",
                    "5",
                    "eval",
                    SNAPSHOT_EXPRESSION,
                    "--formatter=json",
                ],
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=30,
                env=env,
            )
        except subprocess.TimeoutExpired as error:
            raise DeliveryError("broker snapshot query exceeded deadline") from error
        if result.returncode:
            raise DeliveryError("broker snapshot query failed")
        if output.tell() > 32768:
            raise DeliveryError("broker snapshot query output exceeds bound")
        output.seek(0)
        value = operator_units.decode(output.read(32769))
    fields(value, {"result", "node", "value"}, "broker snapshot envelope")
    if (
        value["result"] != "ok"
        or value["node"] != "kf@localhost"
        or not isinstance(value["value"], str)
    ):
        raise DeliveryError("broker snapshot result or node differs")
    return fields(
        operator_units.decode(value["value"].encode()),
        {"queues", "vhost_limits", "users", "permissions"},
        "broker snapshot",
    )
