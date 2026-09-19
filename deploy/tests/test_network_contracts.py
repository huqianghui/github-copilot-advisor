"""Real compiler contracts for the bounded, enforced storage perimeter."""

from test_infra_contracts import compile_template, default, of_type, only, body


SUBSCRIPTION_ID = "/subscriptions/18827cdf-a4e8-4316-a7ba-a4499139a3ec"


def test_network_template_has_one_perimeter_profile_and_subscription_rule():
    template = compile_template(r"deploy\network\infra\main.bicep")
    assert template["$schema"] == "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#"
    assert len(template["resources"]) == 5
    assert default(template, "location") == "southeastasia"
    assert template["parameters"]["location"]["allowedValues"] == ["southeastasia"]
    assert default(template, "inboundSubscriptionId") == SUBSCRIPTION_ID
    assert template["parameters"]["inboundSubscriptionId"]["allowedValues"] == [SUBSCRIPTION_ID]
    only(template, "Microsoft.Network/networkSecurityPerimeters")
    only(template, "Microsoft.Network/networkSecurityPerimeters/profiles")
    rule = only(template, "Microsoft.Network/networkSecurityPerimeters/profiles/accessRules")
    assert rule["properties"] == {
        "direction": "Inbound", "subscriptions": [{"id": "[parameters('inboundSubscriptionId')]"}],
    }


def test_network_associations_are_exactly_two_and_always_enforced():
    template = compile_template(r"deploy\network\infra\main.bicep")
    associations = of_type(template, "Microsoft.Network/networkSecurityPerimeters/resourceAssociations")
    assert len(associations) == 2
    assert {item["properties"]["privateLinkResource"]["id"] for item in associations} == {
        "[parameters('acaStorageId')]", "[parameters('flexStorageId')]",
    }
    assert all(item["properties"]["accessMode"] == "Enforced" for item in associations)
    assert all("profiles" in item["properties"]["profile"]["id"] for item in associations)
    assert not of_type(template, "Microsoft.Storage/storageAccounts")
    assert not of_type(template, "Microsoft.Authorization/roleAssignments")
    assert not of_type(template, "Microsoft.Network/virtualNetworks")


def test_storage_redeploy_never_resets_policy_compatible_intent_to_enabled():
    for target in ("aca", "functions"):
        template = compile_template(fr"deploy\{target}\infra\main.bicep")
        state = body(template, "state")
        storage = only(state, "Microsoft.Storage/storageAccounts")
        assert default(state, "storagePublicNetworkAccess") == "SecuredByPerimeter"
        assert set(state["parameters"]["storagePublicNetworkAccess"]["allowedValues"]) == {"Disabled", "SecuredByPerimeter"}
        assert storage["properties"]["publicNetworkAccess"] == "[parameters('storagePublicNetworkAccess')]"
        assert storage["properties"]["networkAcls"] == {"defaultAction": "Deny", "bypass": "None"}
        assert storage["properties"]["allowSharedKeyAccess"] is False
        assert storage["properties"]["allowBlobPublicAccess"] is False
