targetScope = 'subscription'

@allowed(['aca', 'flex'])
param target string

@allowed(['southeastasia'])
param location string = 'southeastasia'

param environmentName string = 'copilot-advisor-exp'

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-copilot-advisor-${target}-exp'
  location: location
  tags: {
    environment: environmentName
    workload: 'copilot-advisor'
    experiment: target
  }
}

output name string = group.name
output id string = group.id
