"""
An account linking module for the satosa proxy
"""
import json
import logging

import requests
from jwkest.jwk import rsa_load, RSAKey
from jwkest.jws import JWS

from .exception import SATOSAAuthenticationError
from .internal_data import InternalResponse
from .logging_util import satosa_logging
from .response import Redirect

logger = logging.getLogger(__name__)

STATE_KEY = "ACCOUNT_LINKING"


class AccountLinkingModule(object):
    """
    Module for handling account linking and recovery. Uses an external account linking service
    """

    def __init__(self, config, callback_func):
        """
        :type config: satosa.satosa_config.SATOSAConfig
        :type callback_func:
        (satosa.context.Context, satosa.internal_data.InternalResponse) -> satosa.response.Response

        :param config: The SATOSA proxy config
        :param callback_func: Callback function when the linking is done
        """
        self.config = config
        self.callback_func = callback_func
        self.enabled = "ACCOUNT_LINKING" in config and \
                       ("enable" not in config["ACCOUNT_LINKING"] or config["ACCOUNT_LINKING"]["enable"])
        if self.enabled:
            self.proxy_base = config["BASE"]
            self.api_url = config["ACCOUNT_LINKING"]["api_url"]
            self.redirect_url = config["ACCOUNT_LINKING"]["redirect_url"]
            self.signing_key = RSAKey(key=rsa_load(config["ACCOUNT_LINKING"]["sign_key"]), use="sig", alg="RS256")
            self.endpoint = "/handle_account_linking"
            logger.info("Account linking is active")
        else:
            logger.info("Account linking is not active")

    def _handle_al_response(self, context):
        """
        Endpoint for handling account linking service response

        :type context: satosa.context.Context
        :rtype: satosa.response.Response

        :param context: The current context
        :return: response
        """
        saved_state = context.state.get(STATE_KEY)
        internal_response = InternalResponse.from_dict(saved_state)
        return self.manage_al(context, internal_response)

    def manage_al(self, context, internal_response):
        """
        Manage account linking and recovery

        :type context: satosa.context.Context
        :type internal_response: satosa.internal_data.InternalResponse
        :rtype: satosa.response.Response

        :param context:
        :param internal_response:
        :return: response
        """

        if not self.enabled:
            return self.callback_func(context, internal_response)

        issuer = internal_response.auth_info.issuer
        id = internal_response.get_user_id()
        status_code, message = self._get_uuid(context, issuer, id)

        if status_code == 200:
            satosa_logging(logger, logging.INFO, "issuer/id pair is linked in AL service",
                           context.state)
            internal_response.set_user_id(message)
            try:
                context.state.remove(STATE_KEY)
            except KeyError:
                pass
            return self.callback_func(context, internal_response)

        return self._approve_new_id(context, internal_response, message)

    def _approve_new_id(self, context, internal_response, ticket):
        """
        Redirect the user to approve the new id

        :type context: satosa.context.Context
        :type internal_response: satosa.internal_data.InternalResponse
        :type ticket: str
        :rtype: satosa.response.Redirect

        :param context: The current context
        :param internal_response: The internal response
        :param ticket: The ticket given by the al service
        :return: A redirect to approve the new id linking
        """
        satosa_logging(logger, logging.INFO, "A new ID must be linked by the AL service",
                       context.state)
        context.state.add(STATE_KEY, internal_response.to_dict())
        return Redirect("%s/%s" % (self.redirect_url, ticket))

    def _get_uuid(self, context, issuer, id):
        """
        Ask the account linking service for a uuid.
        If the given issuer/id pair is not linked, then the function will return a ticket.
        This ticket should be used for linking the issuer/id pair to the user account

        :type context: satosa.context.Context
        :type issuer: str
        :type id: str
        :rtype: (int, str)

        :param context: The current context
        :param issuer: the issuer used for authentication
        :param id: the given id
        :return: response status code and message
            (200, uuid) or (400, ticket)
        """
        data = {
            "idp": issuer,
            "id": id,
            "redirect_endpoint": "%s/account_linking%s" % (self.proxy_base, self.endpoint)
        }
        jws = JWS(json.dumps(data), alg=self.signing_key.alg).sign_compact([self.signing_key])

        try:
            request = "{}/get_id?jwt={}".format(self.api_url, jws)
            response = requests.get(request)
        except requests.ConnectionError as con_exc:
            msg = "Could not connect to account linking service"
            satosa_logging(logger, logging.CRITICAL, msg, context.state, exc_info=True)
            raise SATOSAAuthenticationError(context.state, msg) from con_exc

        if response.status_code not in [200, 404]:
            msg = "Got status code '%s' from account linking service" % (response.status_code)
            satosa_logging(logger, logging.CRITICAL, msg, context.state)
            raise SATOSAAuthenticationError(context.state, msg)

        return response.status_code, response.text

    def register_endpoints(self):
        """
        Register consent module endpoints

        :rtype: list[(srt, (satosa.context.Context) -> Any)]

        :return: A list of endpoints bound to a function
        """
        return [("^account_linking%s$" % self.endpoint, self._handle_al_response)]
