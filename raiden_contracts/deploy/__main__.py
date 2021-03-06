"""
A simple Python script to deploy compiled contracts.
"""
import json
import logging
from logging import getLogger
from typing import Dict

import click
from eth_utils import denoms, encode_hex, is_address, to_checksum_address
from web3 import HTTPProvider, Web3
from web3.middleware import geth_poa_middleware

from raiden_contracts.constants import (
    CONTRACT_CUSTOM_TOKEN,
    CONTRACT_ENDPOINT_REGISTRY,
    CONTRACT_SECRET_REGISTRY,
    CONTRACT_TOKEN_NETWORK_REGISTRY,
    DEPLOY_SETTLE_TIMEOUT_MAX,
    DEPLOY_SETTLE_TIMEOUT_MIN,
)
from raiden_contracts.contract_manager import (
    ContractManager,
    CONTRACTS_SOURCE_DIRS,
    CONTRACTS_PRECOMPILED_PATH,
)
from raiden_contracts.utils.utils import check_succesful_tx
from raiden_libs.private_contract import PrivateContract
from raiden_libs.utils import get_private_key, private_key_to_address

log = getLogger(__name__)


def validate_address(ctx, param, value):
    if not value:
        return None
    try:
        is_address(value)
        return to_checksum_address(value)
    except ValueError:
        raise click.BadParameter('must be a valid ethereum address')


class ContractDeployer:
    def __init__(
        self,
        web3: Web3,
        private_key: str,
        gas_limit: int,
        gas_price: int=1,
        wait: int=10,
    ):
        self.web3 = web3
        self.private_key = private_key
        self.wait = wait
        owner = private_key_to_address(private_key)
        self.transaction = {'from': owner, 'gas_limit': gas_limit}
        if gas_price != 0:
            self.transaction['gasPrice'] = gas_price * denoms.gwei

        self.contract_manager = ContractManager(CONTRACTS_PRECOMPILED_PATH)

        # Check that the precompiled data is correct
        self.contract_manager = ContractManager(CONTRACTS_SOURCE_DIRS)
        self.contract_manager.checksum_contracts()
        self.contract_manager.verify_precompiled_checksums(CONTRACTS_PRECOMPILED_PATH)

    def deploy(
        self,
        contract_name: str,
        args=None,
    ):
        if args is None:
            args = list()
        contract_interface = self.contract_manager.get_contract(
            contract_name,
        )

        # Instantiate and deploy contract
        contract = self.web3.eth.contract(
            abi=contract_interface['abi'],
            bytecode=contract_interface['bin'],
        )
        contract = PrivateContract(contract)

        # Get transaction hash from deployed contract
        txhash = contract.constructor(*args).transact(
            self.transaction,
            private_key=self.private_key,
        )

        # Get tx receipt to get contract address
        log.debug("Deploying %s txHash=%s" % (contract_name, encode_hex(txhash)))
        receipt = check_succesful_tx(self.web3, txhash, self.wait)
        log.info(
            '{0} address: {1}. Gas used: {2}'.format(
                contract_name,
                receipt['contractAddress'],
                receipt['gasUsed'],
            ),
        )
        return receipt['contractAddress']


@click.group(chain=True)
@click.option(
    '--rpc-provider',
    default='http://127.0.0.1:8545',
    help='Address of the Ethereum RPC provider',
)
@click.option(
    '--private-key',
    required=True,
    help='Path to a private key store',
)
@click.option(
    '--wait',
    default=300,
    help='Max tx wait time in s.',
)
@click.option(
    '--gas-price',
    default=5,
    type=int,
    help='Gas price to use in gwei',
)
@click.option(
    '--gas-limit',
    default=5_500_000,
)
@click.pass_context
def main(
    ctx,
    rpc_provider,
    private_key,
    wait,
    gas_price,
    gas_limit,
):

    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger('web3').setLevel(logging.INFO)
    logging.getLogger('urllib3').setLevel(logging.INFO)

    web3 = Web3(HTTPProvider(rpc_provider, request_kwargs={'timeout': 60}))
    web3.middleware_stack.inject(geth_poa_middleware, layer=0)
    print('Web3 provider is', web3.providers[0])

    private_key = get_private_key(private_key)
    assert private_key is not None
    owner = private_key_to_address(private_key)
    assert web3.eth.getBalance(owner) > 0, 'Account with insuficient funds.'
    deployer = ContractDeployer(
        web3,
        private_key,
        gas_limit,
        gas_price,
        wait,
    )
    ctx.obj = {}
    ctx.obj['deployer'] = deployer
    ctx.obj['deployed_contracts'] = {}
    ctx.obj['token_type'] = 'CustomToken'
    ctx.obj['wait'] = wait


@main.command()
@click.pass_context
def raiden(ctx):
    deployed_contracts = deploy_raiden_contracts(
        ctx.obj['deployer'],
    )
    print(json.dumps(deployed_contracts, indent=4))
    ctx.obj['deployed_contracts'].update(deployed_contracts)


@main.command()
@click.option(
    '--token-supply',
    default=10000000,
    help='Token contract supply (number of total issued tokens).',
)
@click.option(
    '--token-name',
    default=CONTRACT_CUSTOM_TOKEN,
    help='Token contract name.',
)
@click.option(
    '--token-decimals',
    default=18,
    help='Token contract number of decimals.',
)
@click.option(
    '--token-symbol',
    default='TKN',
    help='Token contract symbol.',
)
@click.pass_context
def token(
    ctx,
    token_supply,
    token_name,
    token_decimals,
    token_symbol,
):
    deployer = ctx.obj['deployer']
    token_supply *= 10 ** token_decimals
    deployed_token = deploy_token_contract(
        deployer,
        token_supply,
        token_decimals,
        token_name,
        token_symbol,
        token_type=ctx.obj['token_type'],
    )
    print(json.dumps(deployed_token, indent=4))
    ctx.obj['deployed_contracts'].update(deployed_token)


@main.command()
@click.pass_context
@click.option(
    '--token-address',
    default=None,
    callback=validate_address,
    help='Already deployed token address.',
)
@click.option(
    '--registry-address',
    default=None,
    callback=validate_address,
    help='Address of token network registry',
)
def register(
    ctx,
    token_address,
    registry_address,
):
    token_type = ctx.obj['token_type']
    deployer = ctx.obj['deployer']
    if token_address:
        ctx.obj['deployed_contracts'][token_type] = token_address
    if registry_address:
        ctx.obj['deployed_contracts'][CONTRACT_TOKEN_NETWORK_REGISTRY] = registry_address
    assert CONTRACT_TOKEN_NETWORK_REGISTRY in ctx.obj['deployed_contracts']
    assert token_type in ctx.obj['deployed_contracts']
    abi = deployer.contract_manager.get_contract_abi(CONTRACT_TOKEN_NETWORK_REGISTRY)
    register_token_network(
        web3=deployer.web3,
        private_key=deployer.private_key,
        token_registry_abi=abi,
        token_registry_address=ctx.obj['deployed_contracts'][CONTRACT_TOKEN_NETWORK_REGISTRY],
        token_address=ctx.obj['deployed_contracts'][token_type],
        wait=ctx.obj['wait'],
    )


def deploy_raiden_contracts(
    deployer: ContractDeployer,
):
    """Deploy all required raiden contracts and return a dict of contract_name:address"""
    deployed_contracts = {}

    deployed_contracts[CONTRACT_ENDPOINT_REGISTRY] = deployer.deploy(
        CONTRACT_ENDPOINT_REGISTRY,
    )

    deployed_contracts[CONTRACT_SECRET_REGISTRY] = deployer.deploy(
        CONTRACT_SECRET_REGISTRY,
    )
    deployed_contracts[CONTRACT_TOKEN_NETWORK_REGISTRY] = deployer.deploy(
        CONTRACT_TOKEN_NETWORK_REGISTRY,
        [
            deployed_contracts[CONTRACT_SECRET_REGISTRY],
            int(deployer.web3.version.network),
            DEPLOY_SETTLE_TIMEOUT_MIN,
            DEPLOY_SETTLE_TIMEOUT_MAX,
        ],
    )
    return deployed_contracts


def deploy_token_contract(
    deployer: ContractDeployer,
    token_supply: int,
    token_decimals: int,
    token_name: str,
    token_symbol: str,
    token_type: str='CustomToken',
):
    """Deploy a token contract."""
    deployed_contracts = {}
    deployed_contracts[token_type] = deployer.deploy(
        token_type,
        [token_supply, token_decimals, token_name, token_symbol],
    )

    token_address = deployed_contracts[token_type]
    assert token_address and is_address(token_address)
    token_address = to_checksum_address(token_address)
    return {token_type: token_address}


def register_token_network(
    web3: Web3,
    private_key: str,
    token_registry_abi: Dict,
    token_registry_address: str,
    token_address: str,
    wait=10,
    gas_limit=4000000,
):
    """Register token with a TokenNetworkRegistry contract."""
    token_network_registry = web3.eth.contract(
        abi=token_registry_abi,
        address=token_registry_address,
    )
    token_network_registry = PrivateContract(token_network_registry)
    txhash = token_network_registry.functions.createERC20TokenNetwork(
        token_address,
    ).transact(
        {'gas_limit': gas_limit},
        private_key=private_key,
    )
    log.debug(
        "calling createERC20TokenNetwork(%s) txHash=%s" %
        (
            token_address,
            encode_hex(txhash),
        ),
    )
    receipt = check_succesful_tx(web3, txhash, wait)

    print(
        'TokenNetwork address: {0} Gas used: {1}'.format(
            token_network_registry.functions.token_to_token_networks(token_address).call(),
            receipt['gasUsed'],
        ),
    )


if __name__ == '__main__':
    main()
