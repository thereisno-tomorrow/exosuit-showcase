# Fleet coordinator host example

Give the coordinator its own scoped identity. A named worker is a recipient,
not an authenticated caller. Forward only required context to workers; verify
native pickup and attributable return separately. Missing or ambiguous worker
discovery must block dispatch. No personal bot roster is supplied.
